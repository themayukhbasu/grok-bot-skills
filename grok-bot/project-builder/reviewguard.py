#!/usr/bin/env python3
"""Builder review-fix guard: single-writer state machine per (repo, PR). Stdlib SQLite, BEGIN IMMEDIATE.
Guarantees ONE launcher / ONE completion handler / ONE reply attempt per (comment, epoch) in this DB.
It does NOT guarantee exactly-once GitHub replies: an ambiguous HTTP send blocks and waits for the owner.
PR:      idle -> reserved(epoch,owner) -> active(epoch,bc) -> replying(epoch,handler) -> idle | reserved | blocked
Comment: latest (rev,hash) | done_hash (answered revision) | batch (epoch,hash snapshot in flight)
Reply:   (cid,epoch) sending(token) | sent ; reset only by owner reconcile while blocked."""
import sqlite3, json, argparse, os, re, sys, datetime
DB = os.environ.get("REVIEWGUARD_DB", "/home/box/builder-review-guard/state.db")
MAX_PASSES = int(os.environ.get("REVIEWGUARD_MAX_PASSES", "3"))
# Raw CloudAgent status string (exact, case-sensitive) -> verdict. Only OBSERVED strings are mapped:
# get renders "Status: finished", list renders "[finished,...]" -> pass the bare word "finished".
# Unmapped strings hold. Failure strings are added only once observed (REVIEWGUARD_STATUS_MAP="finished=OK,x=FAIL").
# RUN = observed non-terminal (quiet hold). Unmapped = hold AND alert the owner once.
MAP = dict(x.split("=", 1) for x in os.environ.get("REVIEWGUARD_STATUS_MAP", "finished=OK,running=RUN").split(",") if x)
assert set(MAP.values()) <= {"OK", "FAIL", "RUN"}, MAP
OK = {"OK"}; FAIL = {"FAIL"}
class Bad(Exception): pass
def need(v, rx, what):
    if not isinstance(v, str) or not re.fullmatch(rx, v): raise Bad(f"invalid {what}: {v!r}")
    return v
def ts(v, what="rev"):
    """Canonical UTC only: YYYY-MM-DDTHH:MM:SSZ (GitHub updated_at/created_at), so string order == time order."""
    need(v, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", what)
    try: datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError: raise Bad(f"invalid {what}: {v!r}")
    return v
# CID: issue comment | inline review comment | top-level review body
CID = r"(issue|review|review-body):[1-9]\d*"; HASH = r"[0-9a-f]{64}"; BC = r"bc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SCHEMA = """
CREATE TABLE IF NOT EXISTS pr(repo TEXT, pr INT, state TEXT DEFAULT 'idle', epoch INT DEFAULT 0, owner TEXT,
  bc TEXT, agent TEXT, passes INT DEFAULT 0, reason TEXT, notified INT DEFAULT 0, conflict TEXT, alert TEXT, PRIMARY KEY(repo,pr));
CREATE TABLE IF NOT EXISTS audit(repo TEXT, pr INT, at TEXT DEFAULT CURRENT_TIMESTAMP, cmd TEXT, epoch INT, detail TEXT);
CREATE TABLE IF NOT EXISTS cm(repo TEXT, pr INT, cid TEXT, rev TEXT, hash TEXT, done_hash TEXT,
  b_epoch INT, b_hash TEXT, PRIMARY KEY(repo,pr,cid));
CREATE TABLE IF NOT EXISTS rp(repo TEXT, pr INT, cid TEXT, epoch INT, hash TEXT, state TEXT, token TEXT,
  PRIMARY KEY(repo,pr,cid,epoch));"""

def tx(f):
    def w(a):
        c = sqlite3.connect(DB, timeout=60, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=FULL"); c.executescript(SCHEMA)
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("INSERT OR IGNORE INTO pr(repo,pr) VALUES(?,?)", (a.repo, a.pr))
            r = f(c, a, (a.repo, a.pr)); c.execute("COMMIT"); return r
        except BaseException:
            c.execute("ROLLBACK"); raise
        finally: c.close()
    return w

def st(c, k):
    return dict(zip("state epoch owner bc agent passes reason notified conflict alert".split(),
        c.execute("SELECT state,epoch,owner,bc,agent,passes,reason,notified,conflict,alert FROM pr WHERE repo=? AND pr=?", k).fetchone()))
def pending(c, k):
    return [r[0] for r in c.execute("SELECT cid FROM cm WHERE repo=? AND pr=? AND b_epoch IS NULL "
        "AND hash IS NOT done_hash ORDER BY cid", k)]
def setpr(c, k, **v):
    if "state" in v: v.setdefault("notified", 0); v.setdefault("alert", None)   # each new state may be surfaced once
    c.execute("UPDATE pr SET " + ",".join(f"{x}=?" for x in v) + " WHERE repo=? AND pr=?", (*v.values(), *k))
def block(c, k, reason, **extra):
    setpr(c, k, state="blocked", reason=reason, notified=0)
    return {"action": "blocked", "why": reason, **extra}
def new_batch(c, k, owner, passes):
    e = st(c, k)["epoch"] + 1; p = pending(c, k)
    setpr(c, k, state="reserved", epoch=e, owner=owner, bc=None, agent=None, passes=passes, reason=None)
    c.execute("UPDATE cm SET b_epoch=?, b_hash=hash WHERE repo=? AND pr=? AND b_epoch IS NULL AND hash IS NOT done_hash", (e, *k))
    snap = {cid: h for cid, h in c.execute("SELECT cid,b_hash FROM cm WHERE repo=? AND pr=? AND b_epoch=?", (*k, e))}
    return {"action": "launch", "epoch": e, "comments": p, "snapshot": snap}
def is_(r, **want): return all(r[x] == y for x, y in want.items())

@tx
def enqueue(c, a, k):
    """rev = comment updated_at (ISO, sortable). Older revisions never overwrite newer ones.
    --answered records that a Builder reply exists for THIS revision; it only counts if it is the latest."""
    need(a.cid, CID, "cid"); ts(a.rev); need(a.hash, HASH, "hash")
    if a.body_prefix_bot: return {"action": "skip", "why": "own reply"}
    if a.answered: ts(a.answered, "reply created_at")
    row = c.execute("SELECT rev,hash FROM cm WHERE repo=? AND pr=? AND cid=?", (*k, a.cid)).fetchone()
    if row is None:
        c.execute("INSERT INTO cm(repo,pr,cid,rev,hash) VALUES(?,?,?,?,?)", (*k, a.cid, a.rev, a.hash))
    elif a.rev == row[0] and a.hash != row[1]:
        why = f"{a.cid} rev {a.rev} seen with two different hashes"
        if st(c, k)["state"] == "idle": return block(c, k, why)
        # Lifecycle/ownership untouched: the holder finishes; claim/drain/replies refuse while flagged.
        setpr(c, k, conflict=why, alert=why, notified=0); return {"action": "conflict", "why": why}
    elif a.rev > row[0]:
        c.execute("UPDATE cm SET rev=?,hash=? WHERE repo=? AND pr=? AND cid=?", (a.rev, a.hash, *k, a.cid))
    latest = c.execute("SELECT hash FROM cm WHERE repo=? AND pr=? AND cid=?", (*k, a.cid)).fetchone()[0]
    # Evidence counts only for the latest revision AND a reply created at/after that revision (edits after a reply stay pending).
    if a.answered and latest == a.hash and a.answered >= a.rev:
        c.execute("UPDATE cm SET done_hash=? WHERE repo=? AND pr=? AND cid=?", (a.hash, *k, a.cid))
    return {"action": "recorded", "pending": a.cid in pending(c, k)}

@tx
def claim(c, a, k):
    r = st(c, k)
    if r["state"] != "idle": return {"action": "exit", "why": "held:" + r["state"]}
    if r["conflict"]: return block(c, k, r["conflict"])
    if not pending(c, k): return {"action": "exit", "why": "nothing pending"}
    return new_batch(c, k, a.token, 1)

@tx
def launched(c, a, k):
    if not is_(st(c, k), state="reserved", epoch=a.epoch, owner=a.token): return {"action": "reject", "state": st(c, k)}
    need(a.bc, BC, "bc"); setpr(c, k, state="active", bc=a.bc); return {"action": "ok"}

@tx
def launch_failed(c, a, k):
    """--definite only if the launch API returned an explicit error AND scope=all shows no agent on the branch."""
    if not is_(st(c, k), state="reserved", epoch=a.epoch, owner=a.token): return {"action": "reject", "state": st(c, k)}
    if a.definite: c.execute("UPDATE cm SET b_epoch=NULL,b_hash=NULL WHERE repo=? AND pr=? AND b_epoch=?", (*k, a.epoch))
    return block(c, k, "launch failed" if a.definite else "launch ambiguous: check scope=all for branch agents")

@tx
def complete(c, a, k):
    """Completion lease: first caller with matching epoch+bc becomes the sole reply handler. No expiry, no steal."""
    r = st(c, k)
    if not is_(r, state="active", epoch=a.epoch, bc=a.bc): return {"action": "noop", "why": "stale/duplicate", "state": r["state"]}
    v = MAP.get(a.status)
    if v == "RUN": return {"action": "hold", "why": "running"}
    if v is None:
        if r["alert"] != f"unrecognised agent status {a.status!r}":
            setpr(c, k, alert=f"unrecognised agent status {a.status!r}", notified=0)
        return {"action": "hold", "why": f"status {a.status!r} unrecognised: owner alerted once"}
    setpr(c, k, state="replying", owner=a.token, agent=v)
    return {"action": "handle", "epoch": a.epoch, "agent_ok": v in OK,
            "snapshot": {x: h for x, h in c.execute("SELECT cid,b_hash FROM cm WHERE repo=? AND pr=? AND b_epoch=?", (*k, a.epoch))}}

@tx
def reply_begin(c, a, k):
    r = st(c, k)
    if not is_(r, state="replying", epoch=a.epoch, owner=a.token): return {"action": "reject", "why": "not reply handler"}
    if r["agent"] not in OK: return {"action": "reject", "why": "fix agent failed: no success replies; call finish"}
    if r["conflict"]: return {"action": "reject", "why": "content conflict: no replies; call finish"}
    cm = c.execute("SELECT b_hash FROM cm WHERE repo=? AND pr=? AND cid=? AND b_epoch=?", (*k, a.cid, a.epoch)).fetchone()
    if cm is None or cm[0] != a.hash:   # extra/unknown cid or reply for a different snapshot: fail closed
        return block(c, k, f"agent reply {a.cid} hash {a.hash} not in snapshot of epoch {a.epoch}")
    rp = c.execute("SELECT state FROM rp WHERE repo=? AND pr=? AND cid=? AND epoch=?", (*k, a.cid, a.epoch)).fetchone()
    if rp and rp[0] == "sent": return {"action": "skip", "why": "already sent"}
    if rp: return block(c, k, f"reply {a.cid} ambiguous")
    c.execute("INSERT INTO rp VALUES(?,?,?,?,?,'sending',?)", (*k, a.cid, a.epoch, cm[0], a.token))
    return {"action": "send", "marker": f"<!-- builder-fix:{a.cid}:{a.epoch} -->"}

@tx
def reply_done(c, a, k):
    """--posted: GitHub returned the created reply. --rejected: explicit 4xx, nothing created. Anything else: don't call; finish blocks."""
    if not is_(st(c, k), state="replying", epoch=a.epoch, owner=a.token): return {"action": "reject", "why": "not reply handler"}
    n = c.execute("UPDATE rp SET state=? WHERE repo=? AND pr=? AND cid=? AND epoch=? AND token=? AND state='sending'",
                  ("sent" if a.posted else "rejected", *k, a.cid, a.epoch, a.token)).rowcount
    if a.rejected and n: c.execute("DELETE FROM rp WHERE repo=? AND pr=? AND cid=? AND epoch=?", (*k, a.cid, a.epoch))
    return {"action": "ok" if n else "reject"}

@tx
def finish(c, a, k):
    r = st(c, k)
    if not is_(r, state="replying", epoch=a.epoch, owner=a.token): return {"action": "reject", "why": "not reply handler"}
    if c.execute("SELECT 1 FROM rp WHERE repo=? AND pr=? AND epoch=? AND state='sending'", (*k, a.epoch)).fetchone():
        return block(c, k, "reply ambiguous at finish")
    c.execute("UPDATE cm SET done_hash=(SELECT hash FROM rp WHERE rp.repo=cm.repo AND rp.pr=cm.pr AND rp.cid=cm.cid "
              "AND rp.epoch=cm.b_epoch AND rp.state='sent') WHERE repo=? AND pr=? AND b_epoch=? AND EXISTS(SELECT 1 FROM rp "
              "WHERE rp.repo=cm.repo AND rp.pr=cm.pr AND rp.cid=cm.cid AND rp.epoch=cm.b_epoch AND rp.state='sent')", (*k, a.epoch))
    c.execute("UPDATE cm SET b_epoch=NULL,b_hash=NULL WHERE repo=? AND pr=? AND b_epoch=?", (*k, a.epoch))
    p = pending(c, k)
    if r["agent"] in FAIL: return block(c, k, f"fix agent {r['agent']}", pending=p)
    if r["conflict"]: return block(c, k, r["conflict"], pending=p)
    if not p:
        setpr(c, k, state="idle", owner=None, bc=None, agent=None, passes=0, reason=None); return {"action": "released"}
    if r["passes"] >= MAX_PASSES: return block(c, k, "drain limit reached", pending=p)
    return new_batch(c, k, a.token, r["passes"] + 1)

@tx
def notify_claim(c, a, k):
    """Returns notify exactly once per block; later wakes get quiet."""
    """Once per state/alert: blocked; an alert (unrecognised status, content conflict); or reserved/replying held by
    ANOTHER run's token (maybe a crashed run: owner may `recover`). Informational only; never changes ownership."""
    r = st(c, k)
    if r["notified"]: return {"action": "quiet"}
    if r["state"] == "blocked": why = r["reason"]
    elif r["alert"]: why = r["alert"]
    elif r["state"] in ("reserved", "replying") and r["owner"] != a.token:
        why = f"{r['state']} held by run {r['owner']} (epoch {r['epoch']}); if that run is dead, owner may recover"
    else: return {"action": "quiet"}
    setpr(c, k, notified=1)
    return {"action": "notify", "why": why, "state": r["state"], "epoch": r["epoch"], "bc": r["bc"], "owner": r["owner"], "pending": pending(c, k)}

def quiesced(c, k, a, r):
    """Owner-attested evidence, bound to the stored epoch, agent and handler. The guard cannot check the cloud API:
    it only refuses evidence that names the wrong epoch/agent/handler, and records it in `audit`."""
    if a.epoch != r["epoch"]: return "epoch mismatch"
    if a.agent != (r["bc"] or "none"): return f"agent must be {r['bc'] or 'none'}"
    if a.handler != (r["owner"] or "none"): return f"handler must be {r['owner'] or 'none'}"
    if not a.approved_by.strip() or len(a.evidence.strip()) < 10: return "approved-by and evidence required"
    c.execute("INSERT INTO audit(repo,pr,cmd,epoch,detail) VALUES(?,?,?,?,?)", (*k, a.cmd, a.epoch, json.dumps(
        {"from": r["state"], "agent": a.agent, "handler": a.handler, "approved_by": a.approved_by, "evidence": a.evidence})))

@tx
def recover(c, a, k):
    """Owner-approved: reserved/active/replying -> blocked after the owner checked externally that the stored agent is
    terminal (or that no agent exists) AND the stored handler run is dead and cannot send. No timeouts, no takeover."""
    r = st(c, k)
    if r["state"] not in ("reserved", "active", "replying"): return {"action": "reject", "why": "state " + r["state"]}
    bad = quiesced(c, k, a, r)
    if bad: return {"action": "reject", "why": bad}
    return block(c, k, f"owner recovered from {r['state']}")

@tx
def reconcile_reply(c, a, k):
    """Owner-run only, while blocked, after confirming the old sender is gone (its run ended / no agent alive)
    and reading the thread. --present: the marker reply exists. --absent: it does not and cannot appear later."""
    if st(c, k)["state"] != "blocked": return {"action": "reject", "why": "only while blocked"}
    if a.present: c.execute("UPDATE rp SET state='sent' WHERE repo=? AND pr=? AND cid=? AND epoch=? AND state='sending'", (*k, a.cid, a.epoch))
    else: c.execute("DELETE FROM rp WHERE repo=? AND pr=? AND cid=? AND epoch=? AND state='sending'", (*k, a.cid, a.epoch))
    return {"action": "ok"}

@tx
def unblock(c, a, k):
    """Owner-approved. Refuses while any reply is unreconciled. Sent replies count; everything else goes back to pending."""
    r = st(c, k)
    if not is_(r, state="blocked", epoch=a.epoch): return {"action": "reject", "state": r}
    bad = quiesced(c, k, a, r)
    if bad: return {"action": "reject", "why": bad}
    amb = [x[0] for x in c.execute("SELECT cid FROM rp WHERE repo=? AND pr=? AND state='sending'", k)]
    if amb: return {"action": "reject", "why": "reconcile replies first", "ambiguous": amb}
    c.execute("UPDATE cm SET done_hash=(SELECT hash FROM rp WHERE rp.repo=cm.repo AND rp.pr=cm.pr AND rp.cid=cm.cid "
              "AND rp.epoch=cm.b_epoch) WHERE repo=? AND pr=? AND b_epoch IS NOT NULL AND EXISTS(SELECT 1 FROM rp "
              "WHERE rp.repo=cm.repo AND rp.pr=cm.pr AND rp.cid=cm.cid AND rp.epoch=cm.b_epoch AND rp.state='sent')", k)
    c.execute("UPDATE cm SET b_epoch=NULL,b_hash=NULL WHERE repo=? AND pr=?", k)
    setpr(c, k, state="idle", owner=None, bc=None, agent=None, passes=0, reason=None, notified=0, conflict=None)
    return {"action": "idle", "pending": pending(c, k)}

@tx
def status(c, a, k):
    return {"pr": st(c, k), "pending": pending(c, k),
            "comments": [dict(zip("cid rev hash done_hash b_epoch b_hash".split(), x)) for x in
                         c.execute("SELECT cid,rev,hash,done_hash,b_epoch,b_hash FROM cm WHERE repo=? AND pr=? ORDER BY cid", k)],
            "replies": [dict(zip("cid epoch state".split(), x)) for x in
                        c.execute("SELECT cid,epoch,state FROM rp WHERE repo=? AND pr=? ORDER BY cid,epoch", k)]}

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    def sub(n, f, *xs, flags=()):
        q = s.add_parser(n); q.set_defaults(f=f); q.add_argument("repo"); q.add_argument("pr", type=int)
        for x in xs: q.add_argument(x, type=int if x == "epoch" else str)
        g = q.add_mutually_exclusive_group(required=n in ("reply-done", "reconcile-reply")) if flags else None
        for fl in flags: (g if n in ("reply-done", "reconcile-reply") else q).add_argument(fl, action="store_true")
    sub("enqueue", enqueue, "cid", "rev", "hash", flags=("--body-prefix-bot",))
    s.choices["enqueue"].add_argument("--answered", metavar="REPLY_CREATED_AT", default=None)
    sub("claim", claim, "token"); sub("launched", launched, "epoch", "token", "bc")
    sub("launch-failed", launch_failed, "epoch", "token", flags=("--definite",))
    sub("complete", complete, "epoch", "bc", "status", "token")
    sub("reply-begin", reply_begin, "cid", "epoch", "hash", "token")
    sub("reply-done", reply_done, "cid", "epoch", "token", flags=("--posted", "--rejected"))
    sub("finish", finish, "epoch", "token"); sub("notify-claim", notify_claim, "token")
    sub("reconcile-reply", reconcile_reply, "cid", "epoch", flags=("--present", "--absent"))
    for n, f in (("unblock", unblock), ("recover", recover)):
        sub(n, f, "epoch"); q = s.choices[n]
        q.add_argument("--agent", required=True, help="stored bc, or 'none' if none recorded")
        q.add_argument("--handler", required=True, help="stored owner token, or 'none'")
        q.add_argument("--approved-by", required=True); q.add_argument("--evidence", required=True,
            help="what the owner checked externally, e.g. 'CloudAgent get bc-..: finished; run <token> ended'")
    sub("status", status)
    a = p.parse_args(argv)
    for fl in ("answered", "body_prefix_bot", "definite"): a.__dict__.setdefault(fl, None)
    a.cmd = a.cmd if hasattr(a, "cmd") else None
    try: print(json.dumps(a.f(a)))
    except Bad as e: print(json.dumps({"action": "error", "why": str(e)})); sys.exit(2)
if __name__ == "__main__": main()
