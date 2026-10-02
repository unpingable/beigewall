# FINDINGS — phase 6C: evidence durability

**Status: deterministic fault-injection results (normative) plus a
process-separated scripted run under the durable server; canonical
artifacts byte-identical; no OS/user/namespace work.** The project is to
be renamed BeigeWall before publication; the rename was not performed.
Design and the characterization of the pre-existing failure semantics:
`docs/EVIDENCE_DURABILITY.md`.

## Research question

> Can BeigeWall make accepted effects and authoritative receipts
> recoverable as one coherent evidence history, including failures
> between authorization, effect execution, consumption, and receipt
> persistence?

## What was true before (characterized, not assumed)

The trusted server changed the world, then advanced consumption, then
wrote the effect receipt to memory and then to disk without fsync. Of the
nine failure points the phase names, two violated the evidence claim —
a crash or a write failure after the handler left an effect with no
durable record (and the write-failure case was reported to the client as
an internal error, i.e. as failure) — and one invited duplication: a
retry after a lost response learned nothing on the same grant and landed
a second effect on a fresh one (observed once in phase 5C). Everything
before the handler was already state 1 (nothing happened, receipted or
not). A torn last line refused the restart with no path forward.

## Mechanism chosen (smallest justified)

Durable intent before the handler; commit (or aborting refusal) after,
both with a server-minted `effect_id`; explicit `indeterminate` outcome
and fail-stop when the commit cannot be written; recovery markers on
restart; a receipted `--recover` for a torn tail; write-before-visible
ledger; `prior_effect` on consumed-grant refusals; same-grant retry on the
untrusted side. Not chosen: idempotency keys from the client (the grant
already is one, and client keys are untrusted); prepare/commit with
rollback (the world cannot be rolled back); durable writes for every row
(the recovery-relevant rows are enough and the cost is 25 ms each here).
Only the trusted server's semantics changed; the in-process canonical
reference is byte-identical.

## Fault-injection results (`tests/test_evidence.py`, 19 tests; `tests/test_evidence_client.py`, 5)

| failure point (spec) | injected as | trusted outcome | state | evidence after restart |
|---|---|---|---|---|
| before receipt write | intent write raises ENOSPC | error `evidence`; handler not run; nothing consumed; server fail-stops | 1 | no intent; nothing to recover |
| during receipt write | commit line torn | restart refused; with `--recover` the torn bytes are dropped and receipted | 3 | `effect_indeterminate` for the intent; counted consumed |
| after receipt write, before effect | ledger cut after the intent | — (process gone) | 3 | `effect_indeterminate`; counted consumed |
| before budget consumption | same evidence as above | — | 3 | same |
| after budget consumption | same evidence as above | — | 3 | same |
| during effect execution | handler raises (missing body) | refusal with `effect_id` (abort); nothing consumed; grant still usable; retry is `e-2` | 1 | no marker; not counted |
| during effect execution, abort write fails | handler raises, then the abort row raises ENOSPC | error `evidence` (did not occur, certain: the handler mutates nothing before failing); server fail-stops | 1 (response) / 3 (record) | the intent is left open: `effect_indeterminate`, counted — conservative over-count |
| during effect execution, unexpected exception | handler raises TypeError | error `indeterminate {effect_id, grant_id}` with cause `handler_exception`; consumed; server fail-stops | 3 | `effect_indeterminate`; counted |
| after effect, before final receipt | commit write raises ENOSPC | error `indeterminate {effect_id, grant_id}`; world changed; consumed; grant consumed; server fail-stops | 3 | `effect_indeterminate`; counted consumed; next id `e-2` |
| transport failure after trusted completion | response dropped client-side | client re-sends on the same grant → refusal names `prior_effect committed` → recovered success with the original receipt id; one effect on disk | 2 | — |
| client disappearance after acceptance | client closes; successor continues the task | the consumed grant refuses and names the prior effect; one effect | 2 | — |

Also established: a replayed frame never lands twice in the same task,
another task, or after a restart; effect ids are unique and resume across
restarts (`effect_seq_resumed`); a fresh-grant duplicate is
distinguishable (two ids, two grants) and bounded by the budget but not
prevented; trusted consumption equals intents minus aborts at every point
tested, and the rebuild reproduces it after a restart; corruption other
than a torn tail always refuses to start; a row the disk rejected is never
visible in memory.

The observer (`tests/test_evidence.py::ObserverEvidence`): committed and
aborted effects reconstruct with zero disagreements; an open intent in a
ledger without a later restart is `intent_pending_recovery`; after a
restart it must be resolved by a marker or it is
`intent_unresolved_after_restart`; a commit with no intent, a duplicate
effect id, and an intent past the policy are disagreements; an
indeterminate intent counts against the budget in the observer exactly as
in the trusted rebuild.

## Cost (`results/exploratory/phase6c-ipc-scripted-durable/`, 3 runs)

Outcomes identical to canonical (9 useful, 2 inadmissible, 1 capability
failure; 3 effects per run; 3 intents per run, all committed; every
intent resolved by exactly one commit or abort). After the review, reads,
grant mints and revocations, task ends, restart and policy rows are
fsynced as well as intents, commits, aborts and markers. Per-call IPC
latency over the scripted suite: median 0.67 ms, p90 1.3 ms, max 24 ms
(phase 6: median 0.14 ms, max 0.4 ms); run mean 1.12–1.22 ms (phase 6:
0.15–0.17 ms). Nothing is optimized: one fsync per durable row, and the
durable set is chosen by what an observer's conclusions depend on, not by
cost.

## Hostile review

An independent hostile review (Opus) of the mechanism, with runnable
reproducers, after the fault-injection results above were sealed. The
reproducers are now `tests/test_review_6c.py` (13 tests). Findings and
dispositions:

| # | severity | finding | disposition |
|---|---|---|---|
| 1 | high | `--recover` rewrote the ledger by truncating it to zero first; a crash in that window erased every prior row (ids, closure, consumption reset) | fixed: the partial tail is truncated in place and fsynced, no moment without the complete rows |
| 2 | high | `prior_effect` was attached only to "consumed" refusals; a retry after a lost response on a grant that had since expired was refused "expired" with no outcome, and a fresh grant landed a duplicate | fixed: every refusal on a grant that carried an effect names it; the client treats any `prior_effect` as the outcome |
| 3 | medium | a complete last row without its newline was accepted silently; the next append glued onto it and wedged the ledger for ever | fixed: no newline = torn; refused without `--recover`; with it, a parseable tail is terminated in place, a partial one dropped, both receipted with `action` |
| 4 | medium | a failed abort write escaped as a raw `LedgerError`; `degraded` unset; connection thread died | fixed: explicit `evidence` error (did not occur), fail-stop; reject records tolerate a failed ledger |
| 5 | medium | `degraded` was set only on effect failures; a failed read/grant/narrative row left `hello` healthy and `begin_task` mutating state; narrative/report rows could fill the disk | fixed: degraded on any ledger failure for any op, checked before every op; narrative + report rows bounded at 64 per task |
| 6 | low | intent write landed but fsync failed: client told "did not occur", restart marks indeterminate | accepted, documented: the response is true (the handler never ran) and the record over-counts, never under-counts |
| 7 | low (latent) | only `KeyError`/`EffectError` aborted; any other handler exception left the intent open on a live server, grant unconsumed, retry landed again | fixed: any other exception is an explicit indeterminate outcome, consumed, fail-stop |
| 8 | medium | only intent/commit/abort/marker rows were fsynced; power loss after a delivered read lost the disclosure and reopened the task; `trusted_restart` and `resource_policy` rows could vanish | fixed: `DURABLE_KINDS` (read, grant_mint, grant_revoke, task_end, trusted_restart, resource_policy, lifecycle, markers) fsynced before the response; directory fsynced at creation |
| 9 | low | the exclusive lock was on a sidecar path; a second server via a symlink started and collided ids | fixed: flock on the ledger's own inode |
| 10 | note | recovery trusts every complete row: a same-UID writer can forge an abort or commit and steer recovery | documented, not fixable at one UID: the invariant assumes an honest ledger file (phase 7) |
| 11 | note | trusted rebuild un-counted any intent with an aborted id anywhere; the observer released only an abort following an open intent — disagreement in the unsafe direction on out-of-order or duplicate ids | fixed: order-aware per-intent rebuild matching task and grant; duplicate effect id refuses the start |
| 12 | note | the equivalence filter could not see an open intent | fixed: the test asserts every intent resolved exactly once and intents = canonical effects |

Also noted and fixed: `unittest.main()` sat before the last test class in
`tests/test_evidence.py`; a `ledger_failed` protocol error reached the
driver as "malformed" (now `trusted_unavailable`). Noted, not changed: an
in-process broker with a sink can raise `IndeterminateEffect` with no
intent on a disk failure (it crashes the run — fail-closed, frozen
design).

Confirmed non-findings: client-supplied `effect_id` is a protocol
rejection; duplicates through the server's paths are prevented on the
same grant, across tasks and across restarts; the client retry re-sends
the identical arguments; a torn non-effect row leaves closure intact; a
torn intent never ran its handler; duplicate restart markers cannot
occur; `prior_effect` reaches only the owning task and reveals only ids
every response already reveals.

## Answer

**The three-state invariant is established for the trusted server, with
the stated meaning of each state.** State 1 (did not occur, authority
remains) holds for every failure before the handler and for a handler
failure, with the intent aborted on disk. State 2 (occurred, durable
record) holds when the commit reached the disk, and is what a same-grant
retry recovers after a lost response. State 3 (explicitly indeterminate)
is what remains when the handler ran and the commit did not reach the
disk: the response says so, the server stops acting, the restart records
a marker, consumption counts it, and no retry on the same grant can land
it again. The indeterminate state is honest, not resolvable: in this
spike the world is in-memory and a restart loses it; in a deployment with
real external effects, resolving e-N would require querying the external
system, which is outside the membrane and outside this phase.

**The budget evidence invariant holds**: trusted consumption is intents
minus aborts, the rebuild recomputes exactly that, and the observer
counts the same way, so no reading of the evidence concludes more
authority remains than does.

**What this does not establish**: duplicate prevention across grants
(bounded, not prevented); durability of rows outside `DURABLE_KINDS`
(grant requests and denials, non-abort refusals, narratives, discovery,
ipc rows) across a power loss; anything about a ledger the attacker can
write — the invariant assumes an honest ledger file, which at one UID the
agent process can forge (phase 7's reason to exist); atomicity between the
world and the ledger (not available, not claimed); resolution of an
indeterminate effect (needs the external system).

## Gate for phase 7

No unresolved evidence-integrity problem that undermines the membrane
claims remains: the accepted phase 6B limitation is closed by an explicit
indeterminate state rather than by a stronger promise, and the review's
two high findings (a recovery path that could erase the ledger; a retry
that could not learn a committed outcome after expiry) are fixed and
regression-tested. The one open item is the honest-ledger assumption,
which is exactly the OS boundary question. The OS boundary experiment may
begin.
