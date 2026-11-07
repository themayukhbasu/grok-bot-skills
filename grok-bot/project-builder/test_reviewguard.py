"""Every guard call is a separate OS process; concurrent cases run them in parallel."""
import hashlib, re, json, os, subprocess, sys, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
def H(x): return hashlib.sha256(x.encode()).hexdigest()
def C(x): return x if ":" in x else "review:" + x
def T_(x): return {"t1": "2026-11-07T00:00:01Z", "t2": "2026-11-07T00:00:02Z", "t3": "2026-11-07T00:00:03Z"}.get(x, x)
def B(n): return "bc-%08d-0000-0000-0000-000000000000" % n
def norm(a):   # short test aliases -> typed production inputs
    a = list(map(str, a))
    if a[0] == "enqueue":
        a[1], a[2], a[3] = C(a[1]), T_(a[2]), H(a[3])
        if "--answered" in a: i = a.index("--answered"); (a.__setitem__(i + 1, T_(a[i + 1])) if i + 1 < len(a) and a[i + 1].startswith("t") else a.insert(i + 1, "2026-11-07T00:00:09Z"))
    if a[0] in ("reply-begin", "reply-done", "reconcile-reply"): a[1] = C(a[1])
    if a[0] == "reply-begin" and len(a) == 4: a.insert(3, "AUTO")
    return [B(int(x[3:])) if x.startswith("bc-") and x[3:].isdigit() else x for x in a]
HERE = os.path.dirname(os.path.abspath(__file__)); R, P = "themayukhbasu/peekaboo", "24"
class T(unittest.TestCase):
    def setUp(s): s.env = dict(os.environ, REVIEWGUARD_DB=tempfile.mktemp(suffix=".db"), REVIEWGUARD_MAX_PASSES="2",
                               REVIEWGUARD_STATUS_MAP="finished=OK,running=RUN,FAILED_X=FAIL")
    def g(s, *a, rc=0):
        a = norm(a)
        if a[0] == "reply-begin" and a[3] == "AUTO":   # use the real snapshot hash for this cid
            a[3] = {x["cid"]: x["b_hash"] for x in s.raw("status")["comments"] if x["b_hash"]}.get(a[1], H("none"))
        out = json.dumps(s.raw(*a, rc=rc))   # typed outputs -> short aliases for the original assertions
        out = re.sub(r'"review:(\d+)"', r'"\1"', out); out = re.sub(r"bc-0*(\d+)-0000-0000-0000-000000000000", r"bc-\1", out)
        return json.loads(out)
    def raw(s, *a, rc=0):
        o = subprocess.run([sys.executable, HERE + "/reviewguard.py", a[0], R, P, *map(str, a[1:])],
                           env=s.env, capture_output=True, text=True)
        s.assertEqual(o.returncode, rc, o.stderr + o.stdout); return json.loads(o.stdout)
    def par(s, calls):
        with ThreadPoolExecutor(len(calls)) as x: return list(x.map(lambda c: s.g(*c), calls))
    def st(s): return s.g("status")
    def ev(s, cmd, e, agent=None, handler=None, by="owner", evidence="checked CloudAgent get + run ended"):
        r = s.st()["pr"]; agent = agent or (r["bc"] or "none"); handler = handler or (r["owner"] or "none")
        return s.g(cmd, e, "--agent", agent, "--handler", handler, "--approved-by", by, "--evidence", evidence)
    def ub(s, e, **kw): return s.ev("unblock", e, **kw)
    def active(s, cids, bc="bc-1"):
        for c in cids: s.g("enqueue", c, "t1", "h" + c)
        e = s.g("claim", "w0")["epoch"]; s.g("launched", e, "w0", bc); return e
    def handle(s, e, bc="bc-1", tok="h"):
        s.assertEqual(s.g("complete", e, bc, "finished", tok)["action"], "handle")
    def post(s, e, cids, tok="h"):
        for c in cids:
            s.assertEqual(s.g("reply-begin", c, e, tok)["action"], "send"); s.g("reply-done", c, e, tok, "--posted")

    def test_five_concurrent_claims_one_launch(s):
        for c in "1234": s.g("enqueue", c, "t1", "h")
        acts = [r["action"] for r in s.par([("claim", f"w{i}") for i in range(5)])]
        s.assertEqual(acts.count("launch"), 1)
    def test_owner_token_cas(s):
        s.g("enqueue", "1", "t1", "h"); e = s.g("claim", "a")["epoch"]
        s.assertEqual(s.g("launched", e, "b", "bc-x")["action"], "reject")
    def test_enqueue_while_active_drains(s):
        e = s.active(["1"]); s.g("enqueue", "2", "t1", "h2")
        s.assertEqual(s.g("claim", "w9")["action"], "exit"); s.handle(e); s.post(e, ["1"])
        r = s.g("finish", e, "h"); s.assertEqual((r["action"], r["comments"]), ("launch", ["2"]))
    def test_edited_during_active_stays_pending(s):
        e = s.active(["1"]); s.g("enqueue", "1", "t2", "NEW")
        s.g("enqueue", "1", "t1", "h1", "--answered")       # stale evidence for old revision: ignored
        s.handle(e); snap = s.g("status")["comments"][0]; s.assertEqual(snap["hash"], H("NEW"))
        s.post(e, ["1"]); r = s.g("finish", e, "h")
        s.assertEqual((r["action"], r["comments"], r["snapshot"]), ("launch", ["1"], {"1": H("NEW")}))
    def test_older_rev_never_overwrites(s):
        s.g("enqueue", "1", "t2", "NEW"); s.g("enqueue", "1", "t1", "OLD")
        s.assertEqual(s.st()["comments"][0]["hash"], H("NEW"))
    def test_duplicate_concurrent_completion_one_handler(s):
        e = s.active(["1"])
        acts = [r["action"] for r in s.par([("complete", e, "bc-1", "finished", f"c{i}") for i in range(5)])]
        s.assertEqual((acts.count("handle"), acts.count("noop")), (1, 4))
    def test_concurrent_reply_begin_single_send(s):
        e = s.active(["1"]); s.handle(e)
        acts = [r["action"] for r in s.par([("reply-begin", "1", e, "h")] * 5)]
        s.assertEqual(acts.count("send"), 1); s.assertNotIn("skip", acts)   # others block: ambiguous, no 2nd send
        s.assertEqual(s.st()["pr"]["state"], "blocked")
    def test_non_handler_cannot_reply_or_reset(s):
        e = s.active(["1"]); s.handle(e); s.assertEqual(s.g("reply-begin", "1", e, "h")["action"], "send")
        s.assertEqual(s.g("reply-begin", "1", e, "intruder")["action"], "reject")
        s.assertEqual(s.g("reply-done", "1", e, "intruder", "--rejected")["action"], "reject")
        s.assertEqual(s.g("reconcile-reply", "1", e, "--absent")["action"], "reject")   # not blocked: refused
        s.assertEqual(s.st()["replies"][0]["state"], "sending")
    def test_concurrent_reconcile_while_sending_no_duplicate(s):
        e = s.active(["1"]); s.handle(e); s.g("reply-begin", "1", e, "h")
        r = s.par([("reply-done", "1", e, "x", "--rejected"), ("reconcile-reply", "1", e, "--absent"), ("reply-begin", "1", e, "h")])
        s.assertEqual(r[0]["action"], "reject"); s.assertNotEqual(r[2]["action"], "send")
        s.assertEqual(s.st()["replies"][0]["state"], "sending")   # in-flight send never reset by others
    def test_ambiguous_reply_blocks_finish_and_unblock_requires_reconcile(s):
        e = s.active(["1"]); s.handle(e); s.g("reply-begin", "1", e, "h")    # crash: no reply-done
        s.assertEqual(s.g("finish", e, "h")["action"], "blocked")
        s.assertEqual(s.ub(e)["action"], "reject")
        s.g("reconcile-reply", "1", e, "--present"); r = s.ub(e)
        s.assertEqual((r["action"], r["pending"]), ("idle", []))
    def test_reconcile_absent_requeues(s):
        e = s.active(["1"]); s.handle(e); s.g("reply-begin", "1", e, "h"); s.g("finish", e, "h")
        s.g("reconcile-reply", "1", e, "--absent"); s.assertEqual(s.ub(e)["pending"], ["1"])
    def test_finished_without_replies_requeues(s):
        e = s.active(["1", "2"]); s.handle(e); s.post(e, ["1"])          # handler posted only one
        r = s.g("finish", e, "h"); s.assertEqual(r["comments"], ["2"])
    def test_stale_completion_after_successor(s):
        e = s.active(["1"]); s.g("enqueue", "2", "t1", "h"); s.handle(e); s.post(e, ["1"])
        n = s.g("finish", e, "h")["epoch"]; s.g("launched", n, "h", "bc-2")
        s.assertEqual(s.g("complete", e, "bc-1", "finished", "late")["action"], "noop")
        s.assertEqual(s.g("finish", e, "h")["action"], "reject"); s.assertEqual(s.st()["pr"]["bc"], "bc-2")
    def test_ambiguous_launch_blocks_notify_once(s):
        s.g("enqueue", "1", "t1", "h"); e = s.g("claim", "a")["epoch"]; s.g("launch-failed", e, "a")
        s.g("enqueue", "2", "t1", "h"); s.assertEqual(s.g("claim", "b")["action"], "exit")
        acts = [r["action"] for r in s.par([("notify-claim", "obs")] * 4)]
        s.assertEqual(acts.count("notify"), 1)
    def test_failed_agent_blocks_not_relaunch(s):
        e = s.active(["1"]); s.assertEqual(s.g("complete", e, "bc-1", "FAILED_X", "h")["agent_ok"], False)
        r = s.g("finish", e, "h"); s.assertEqual((r["action"], r["pending"]), ("blocked", ["1"]))
    def test_nonterminal_status_holds(s):
        e = s.active(["1"]); s.assertEqual(s.g("complete", e, "bc-1", "RUNNING", "h")["action"], "hold")
    def test_late_enqueue_racing_finish_not_lost(s):
        for _ in range(8):
            s.setUp(); e = s.active(["1"]); s.handle(e); s.post(e, ["1"])
            r, _ = s.par([("finish", e, "h"), ("enqueue", "2", "t1", "h")])
            if r["action"] == "released": s.assertEqual(s.g("claim", "w")["comments"], ["2"])
            else: s.assertEqual(r["comments"], ["2"])
    def test_drain_limit_blocks_with_pending_visible(s):
        e = s.active(["1"]); s.g("enqueue", "2", "t1", "h"); s.handle(e); s.post(e, ["1"])
        e = s.g("finish", e, "h")["epoch"]; s.g("launched", e, "h", "bc-2"); s.g("enqueue", "3", "t1", "h")
        s.handle(e, "bc-2"); s.post(e, ["2"]); r = s.g("finish", e, "h")
        s.assertEqual((r["action"], r["pending"]), ("blocked", ["3"]))
        s.assertEqual(s.g("notify-claim", "obs")["pending"], ["3"])
    def test_restart_persistence(s):
        s.active(["1"]); s.assertEqual(s.st()["pr"]["state"], "active"); s.assertEqual(s.g("claim", "z")["action"], "exit")
    # ---- regression tests added in builder audit (2026-11-07) ----
    def test_unobserved_status_strings_hold(s):
        s.env["REVIEWGUARD_STATUS_MAP"] = "finished=OK"; e = s.active(["1"])
        for x in ("FINISHED", "Finished", "ERROR", "Status: finished"):
            s.assertEqual(s.g("complete", e, "bc-1", x, "h")["action"], "hold")
        s.assertEqual(s.g("complete", e, "bc-1", "finished", "h")["action"], "handle")
    def test_completion_for_other_agent_id_noop(s):
        e = s.active(["1"]); s.assertEqual(s.g("complete", e, "bc-7", "finished", "h")["action"], "noop")
        s.assertEqual(s.st()["pr"]["state"], "active")
    def test_same_rev_different_hash_blocks(s):
        s.g("enqueue", "1", "t1", "a"); r = s.g("enqueue", "1", "t1", "b")
        s.assertEqual(r["action"], "blocked"); s.assertEqual(s.g("claim", "w")["action"], "exit")
    def test_invalid_inputs_rejected_no_state(s):
        for bad in (("enqueue", "1", "2026-11-07T00:00:01Z", "x" * 64), ("enqueue", "42", "2026-11-07T00:00:01Z", H("a")),
                    ("enqueue", "review:1", "2026-11-07 00:00:01", H("a")), ("enqueue", "review:1", "2026-11-07T00:00:01+05:30", H("a")),
                    ("enqueue", "review:1", "2026-13-07T00:00:01Z", H("a"))):
            s.assertEqual(s.raw(*bad, rc=2)["action"], "error")
        s.assertEqual(s.st()["comments"], [])
        s.g("enqueue", "1", "t1", "a"); e = s.g("claim", "w")["epoch"]
        s.assertEqual(s.raw("launched", e, "w", "bc-1", rc=2)["action"], "error")
        s.assertEqual(s.st()["pr"]["state"], "reserved")
    def test_review_body_namespace_distinct_and_validated(s):
        for c in ("issue:5", "review:5", "review-body:5"): s.g("enqueue", c, "t1", "a")
        s.assertEqual(s.st()["pending"], ["issue:5", "review-body:5", "5"])   # "5" = alias of review:5
        for bad in ("reviewbody:5", "review-body:", "review-body:0", "review-body:5x", "pr:5"):
            s.assertEqual(s.raw("enqueue", bad, "2026-11-07T00:00:01Z", H("a"), rc=2)["action"], "error")
    def test_issue_and_review_ids_distinct(s):
        s.g("enqueue", "issue:5", "t1", "a"); s.g("enqueue", "review:5", "t1", "b")
        s.assertEqual(s.st()["pending"], ["issue:5", "5"])   # alias of review:5
    def test_reply_wrong_hash_or_extra_cid_blocks_no_send(s):
        e = s.active(["1", "2"]); s.handle(e)
        s.assertEqual(s.g("reply-begin", "1", e, H("other"), "h")["action"], "blocked")
        s.assertEqual(s.g("reply-begin", "2", e, "h")["action"], "reject")   # PR now blocked: nothing else sends
        s.setUp(); e = s.active(["1"]); s.handle(e)
        s.assertEqual(s.g("reply-begin", "9", e, "h")["action"], "blocked"); s.assertEqual(s.st()["replies"], [])
    def test_failed_agent_cannot_send_replies(s):
        e = s.active(["1"]); s.g("complete", e, "bc-1", "FAILED_X", "h")
        s.assertEqual(s.g("reply-begin", "1", e, "h")["action"], "reject"); s.assertEqual(s.st()["replies"], [])
    def test_reply_done_fenced_by_handler_state(s):
        e = s.active(["1"]); s.handle(e); s.g("reply-begin", "1", e, "h"); s.g("finish", e, "h")   # blocked (sending)
        s.assertEqual(s.g("reply-done", "1", e, "h", "--posted")["action"], "reject")
        s.assertEqual(s.st()["replies"][0]["state"], "sending")
    def test_answered_evidence_must_postdate_revision(s):
        s.g("enqueue", "1", "t2", "a", "--answered", "t1"); s.assertEqual(s.st()["pending"], ["1"])
        s.setUp(); s.g("enqueue", "1", "t2", "a", "--answered", "t3"); s.assertEqual(s.st()["pending"], [])
    # ---- round 2 (dr eggbot NO-GO findings) ----
    def conflict_in(s, phase):
        e = s.active(["1"]) if phase != "reserved" else None
        if phase == "reserved": s.g("enqueue", "1", "t1", "h1"); e = s.g("claim", "w0")["epoch"]
        if phase == "replying": s.handle(e)
        before = s.st()["pr"]
        s.assertEqual(s.g("enqueue", "1", "t1", "DIFFERENT")["action"], "conflict")
        after = s.st()["pr"]
        for x in ("state", "epoch", "owner", "bc"): s.assertEqual(after[x], before[x], x)   # ownership preserved
        s.assertEqual(s.ub(e)["action"], "reject")                                          # not blocked
        s.assertEqual(s.g("claim", "w9")["action"], "exit")
        return e
    def test_conflict_during_reserved(s):
        e = s.conflict_in("reserved"); s.assertEqual(s.g("launched", e, "w0", "bc-1")["action"], "ok")
        s.assertEqual(s.g("complete", e, "bc-1", "finished", "h")["action"], "handle")
        s.assertEqual(s.g("reply-begin", "1", e, "h")["action"], "reject")
        s.assertEqual(s.g("finish", e, "h")["action"], "blocked"); s.assertEqual(s.st()["pr"]["bc"], "bc-1")
    def test_conflict_during_active_then_unblock_claim(s):
        e = s.conflict_in("active")
        s.assertEqual(s.g("notify-claim", "obs")["action"], "notify"); s.assertEqual(s.g("notify-claim", "obs")["action"], "quiet")
        s.handle(e); s.assertEqual(s.g("reply-begin", "1", e, "h")["action"], "reject")
        s.assertEqual(s.g("finish", e, "h")["action"], "blocked")
        s.assertEqual(s.ub(e, agent="bc-9")["action"], "reject")                          # wrong agent
        s.assertEqual(s.ub(e, handler="x")["action"], "reject")                           # wrong handler
        s.assertEqual(s.ub(e)["action"], "idle"); s.assertEqual(s.st()["pr"]["conflict"], None)
        s.assertEqual(s.g("claim", "w2")["epoch"], e + 1)
    def test_conflict_during_replying(s):
        e = s.conflict_in("replying"); s.assertEqual(s.g("reply-begin", "1", e, "h")["action"], "reject")
        r = s.g("finish", e, "h"); s.assertEqual((r["action"], r["pending"]), ("blocked", ["1"]))
    def test_conflict_when_idle_blocks_and_claim_refuses(s):
        s.g("enqueue", "1", "t1", "a"); s.assertEqual(s.g("enqueue", "1", "t1", "b")["action"], "blocked")
    def test_dead_replying_handler_notify_then_owner_recover(s):
        e = s.active(["1"]); s.handle(e)                                   # handler h then crashes
        s.assertEqual(s.g("notify-claim", "h")["action"], "quiet")         # own run: no self-alert
        r = s.g("notify-claim", "obs"); s.assertEqual((r["action"], r["state"], r["owner"]), ("notify", "replying", "h"))
        s.assertEqual(s.g("notify-claim", "obs2")["action"], "quiet")
        s.assertEqual(s.ev("recover", e, handler="other")["action"], "reject")
        s.assertEqual(s.ev("recover", e, evidence="")["action"], "reject")
        s.assertEqual(s.ev("recover", e + 1)["action"], "reject")
        s.assertEqual(s.st()["pr"]["state"], "replying")                    # nothing stolen by rejected attempts
        s.assertEqual(s.ev("recover", e)["action"], "blocked")
        s.assertEqual(s.g("reply-begin", "1", e, "h")["action"], "reject")   # old handler fenced out
        s.assertEqual(s.ub(e)["pending"], ["1"])
        s.assertEqual(len(s.raw("status")["pr"]), 10)
    def test_recover_replying_with_inflight_reply_needs_reconcile(s):
        e = s.active(["1"]); s.handle(e); s.g("reply-begin", "1", e, "h"); s.ev("recover", e)
        s.assertEqual(s.ub(e)["action"], "reject"); s.g("reconcile-reply", "1", e, "--present")
        s.assertEqual(s.ub(e)["pending"], [])
    def test_dead_reserved_launcher_recover(s):
        s.g("enqueue", "1", "t1", "a"); e = s.g("claim", "w0")["epoch"]
        s.assertEqual(s.g("notify-claim", "obs")["action"], "notify")
        s.assertEqual(s.ev("recover", e, agent="bc-1")["action"], "reject")  # no agent recorded: must say none
        s.assertEqual(s.ev("recover", e)["action"], "blocked"); s.assertEqual(s.g("launched", e, "w0", "bc-1")["action"], "reject")
    def test_ambiguous_launch_unblock_requires_bound_evidence(s):
        s.g("enqueue", "1", "t1", "a"); e = s.g("claim", "a")["epoch"]; s.g("launch-failed", e, "a")
        o = subprocess.run([sys.executable, HERE + "/reviewguard.py", "unblock", R, P, str(e)], env=s.env, capture_output=True, text=True)
        s.assertEqual(o.returncode, 2)                                     # argparse: evidence flags required
        s.assertEqual(s.ub(e, handler="none")["action"], "reject")
        s.assertEqual(s.ub(e, evidence="ok")["action"], "reject")
        s.assertEqual(s.st()["pr"]["state"], "blocked")
        s.assertEqual(s.ub(e)["action"], "idle")
    def test_running_quiet_unrecognised_alerts_once(s):
        e = s.active(["1"])
        for _ in range(3): s.assertEqual(s.g("complete", e, "bc-1", "running", "h")["action"], "hold")
        s.assertEqual(s.g("notify-claim", "obs")["action"], "quiet")
        for _ in range(3): s.assertEqual(s.g("complete", e, "bc-1", "weird", "h")["action"], "hold")
        s.assertEqual(s.g("notify-claim", "obs")["why"], "unrecognised agent status 'weird'")
        s.g("complete", e, "bc-1", "weird", "h"); s.assertEqual(s.g("notify-claim", "obs")["action"], "quiet")
        s.assertEqual(s.st()["pr"]["state"], "active")
    def test_bootstrap_existing_answers_and_own_reply(s):
        s.g("enqueue", "1", "t1", "h", "--answered"); s.g("enqueue", "9", "t1", "h", "--body-prefix-bot")
        s.assertEqual(s.g("claim", "a")["why"], "nothing pending")
        s.g("enqueue", "1", "t2", "h2"); s.assertEqual(s.st()["pending"], ["1"])
unittest.main()
