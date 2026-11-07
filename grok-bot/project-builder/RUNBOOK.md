# Builder review-fix guard: runbook (installable source; default install /home/box/builder-review-guard)
Guarantee: one launcher, one completion handler and one reply attempt per (comment, epoch) on this box.
NOT exactly-once on GitHub: an ambiguous reply POST blocks the PR until the owner reconciles. Fail closed throughout.
Provenance: design and first candidate by dr eggbot; audit fixes and review-body namespace added in Builder review, 2026-11-07.

Setup: `install -D reviewguard.py /home/box/builder-review-guard/reviewguard.py`;
`export REVIEWGUARD_DB=/home/box/builder-review-guard/state.db G="python3 /home/box/builder-review-guard/reviewguard.py"`
(same absolute DB in EVERY run). TOKEN = unique id per orchestration run (`uuidgen`), reused for every call in that run.

## Input contract (anything else: exit 2, `{"action":"error"}`, no state change -> stop, do not guess)
- CID: `review:<id>` (pulls/comments, inline), `issue:<id>` (issues/comments, conversation) or
  `review-body:<review id>` (substantive top-level review text; REV = submitted_at, HASH = sha256 of the review body).
  A review body that only routes to its inline comments ("check the comments") gets no item.
- A claimed CID+HASH must map to the exact immutable source text given to the agent. If the comment is edited,
  never substitute the latest text under the old hash: the edit is a new revision and goes through enqueue.
- REV: comment `updated_at` exactly `YYYY-MM-DDTHH:MM:SSZ` (UTC, as GitHub returns). HASH: sha256 hex of the UTF-8 body as returned.
- Same REV with a different HASH (recovery: fresh-read the comment from GitHub, the owner reconciles which content is authoritative,
  then unblock and re-enqueue the fresh rev/hash; never refix or answer the stale hash):
   if idle -> `blocked`; otherwise `conflict` (state/epoch/agent/handler untouched; the PR is
  flagged, the owner alerted once, claims refuse, the holder sends no replies, `finish` then blocks).
- BC: full `bc-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.
- STATUS: the bare word CloudAgent shows (get: `Status: finished`; list: `[finished,...]` -> pass `finished`).
  Default `REVIEWGUARD_STATUS_MAP=finished=OK,running=RUN` (exact, case-sensitive). `finished` observed; `running` as observed in
  CloudAgent scope=all output. RUN -> quiet `hold`. Any unmapped string -> `hold`
  plus a once-only owner alert. No FAIL strings mapped until observed (`...,<seen>=FAIL`, identical in every run).

## Bootstrap already-answered comments (once per PR, before enabling)
Prepare a local evidence JSON (kept outside the skill) from a fresh read of the PR: for each human comment,
typed CID, REV, HASH, and every later reply starting with the configured bot prefix (BOT_PREFIX, e.g. `<Bot name>: Builder:`) (id, author, created_at, url, sha256).
For each comment whose latest revision has a reply created at/after REV:
`$G enqueue REPO PR CID REV HASH --answered REPLY_CREATED_AT`. Routing-only review bodies get no item.
Then `$G status REPO PR` must show pending [] (or only genuinely unanswered comments) and state idle.

## Notify on every exit
Before ANY exit of a wake (claim exit, complete noop/hold, reject, blocked, helper error), if the DB is usable run
`$G notify-claim REPO PR $TOKEN` and send the owner the message only on `notify`. Helper unusable: tell the owner once that the guard is down; never fall back.

## Comment wake
0. Missed-completion recovery: if `$G status` shows `active`, read THAT recorded bc's status (CloudAgent get bc);
   if it maps terminal, run the completion wake with that bc (it is the recorded agent's own status, not a substitution).
1. For the event comment and each unresolved comment fetched: `$G enqueue REPO PR CID REV HASH`
   (`--body-prefix-bot` if the body starts with BOT_PREFIX). `blocked` -> step 5.
2. `$G claim REPO PR $TOKEN` -> `exit`: stop silently. `launch`: epoch, comments, snapshot {cid: hash}.
3. Launch ONE cloud agent on the PR head branch: fix only the snapshot comments; push code; DO NOT reply on GitHub;
   end with JSON `{"replies":[{"cid":...,"hash":...,"body":...}]}` echoing cid+hash from the snapshot.
4. Agent id returned -> `$G launched REPO PR EPOCH $TOKEN bc-...`.
   No id/error -> check CloudAgent scope=all for this branch; only if none AND an explicit error:
   `$G launch-failed REPO PR EPOCH $TOKEN --definite`; else `$G launch-failed REPO PR EPOCH $TOKEN`.
5. `$G notify-claim REPO PR $TOKEN` -> `notify`: one message to the owner with why/state/epoch/bc/owner/pending. `quiet`: nothing.
   Fires once per state change for: blocked; an alert (conflict, unrecognised status); reserved/replying held by ANOTHER run's token
   (possibly a dead run - informational only, never take over).

## Completion wake (may arrive in any run)
1. BC = the agent id IN THE COMPLETION EVENT. Never take bc from `$G status` and never substitute it.
   EPOCH from `$G status`; STATUS from CloudAgent get for BC.
2. `$G complete REPO PR EPOCH BC STATUS $TOKEN` -> `noop` (other/stale agent or duplicate) / `hold`: stop. `handle`: sole reply handler.
3. Only if `agent_ok` is true: for each reply in the agent's final JSON: `$G reply-begin REPO PR CID EPOCH HASH $TOKEN`
   - Where: `review:` -> reply in that inline thread; `issue:` and `review-body:` -> a new PR conversation comment
     quoting/linking the source (review bodies cannot be replied to inline; never pretend it is a thread reply).
   - `send`: post body + marker once; 2xx with reply id: `reply-done ... --posted`; explicit 4xx: `--rejected`; timeout/5xx/unknown: call nothing.
   - `skip`: already sent. `blocked` (cid/hash not in snapshot) or `reject`: stop replying.
   Unparseable final JSON: send nothing, go to 4. If `agent_ok` false: send nothing (reply-begin also rejects).
4. `$G finish REPO PR EPOCH $TOKEN` -> `launch` (next pass: comment-wake step 3) | `released` | `blocked`; then comment-wake step 5.
   Unanswered snapshot comments return to pending; a failed agent blocks with them listed in `pending`.

## Owner recovery (owner-approved only; the guard cannot check the cloud API)
The flags are the owner's attestation of checks done outside the guard. The guard only refuses evidence that names
the wrong epoch, agent or handler, or is empty, and writes it to the `audit` table. A flag is not an observation.
Busy is not dead: reserved/replying/active held by another run means busy unless the owner has evidence it ended.
Common flags: `EPOCH --agent <stored bc|none> --handler <stored owner token|none> --approved-by <who> --evidence "<what was checked>"`.
- Stuck reserved/active/replying (dead run, unknown agent): the owner confirms CloudAgent get for the stored bc is terminal
  (or scope=all shows no agent for the branch when bc is none) AND the stored handler run has ended and cannot send.
  Then `$G recover REPO PR <flags>` -> `blocked`. The old handler is now fenced out of every call.
- Blocked: read the thread for marker `<!-- builder-fix:CID:EPOCH -->` and `$G reconcile-reply REPO PR CID EPOCH --present|--absent`
  for each in-flight reply, then `$G unblock REPO PR <flags>` -> `idle` (sent replies count; everything else returns to pending;
  the conflict flag clears). Unblock refuses while any reply is unreconciled.
- An ambiguous launch (bc none) needs `--agent none` and evidence that scope=all shows no agent on the branch.
  If an agent did start, wait for it to be terminal before unblocking, or its pushes may race the next agent.
Never infer --absent or quiescence from one quick read while a sender may still run. No timeouts, no lease stealing.

## State machine
idle -claim-> reserved -launched-> active -complete(OK|FAIL)-> replying -finish-> idle | reserved (drain) | blocked
reserved -launch-failed-> blocked. reserved|active|replying -recover(owner)-> blocked. blocked -unblock(owner)-> idle.
idle -enqueue conflict-> blocked; elsewhere conflict = flag only. Holds (RUN/unmapped) stay active.

## Outputs (JSON, one line)
enqueue: recorded{pending}|skip|blocked. claim: exit{why}|launch{epoch,comments,snapshot}. launched/launch-failed: ok|reject|blocked.
complete: noop|hold|handle{epoch,agent_ok,snapshot}. reply-begin: send{marker}|skip|reject|blocked. reply-done: ok|reject.
finish: reject|released|launch|blocked{why,pending}. notify-claim: quiet|notify{why,state,epoch,bc,owner,pending}.
recover: reject{why}|blocked. unblock: reject{why|state|ambiguous}|idle{pending}. enqueue may also return conflict{why}.
status: {pr{state,epoch,owner,bc,agent,passes,reason,notified,conflict,alert},pending,comments,replies}.

## Sidecar sync
This runbook, reviewguard.py and test_reviewguard.py ship as the project-builder skill's sidecar. Install with
`install -D` into /home/box/builder-review-guard/ and run the tests there; a fresh state.db is created on first call.
Per-PR bootstrap evidence and state.db are local, never synced.
