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
failure; 3 effects per run; 3 intents per run, all committed). Per-call
IPC latency: non-effect calls median 0.19 ms (phase 6: 0.14 ms); effect
calls up to 4–28 ms (two fsyncs on a disk where fsync is ≈25 ms); run
mean 0.65–0.90 ms (phase 6: 0.15–0.17 ms), entirely from the effect
calls.

REVIEW_PLACEHOLDER

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
(bounded, not prevented); durability of non-effect rows across a crash;
anything about a ledger the attacker can write (one UID — phase 7);
atomicity between the world and the ledger (not available, not claimed).

## Gate for phase 7

No unresolved evidence-integrity problem that undermines the membrane
claims was found: the accepted phase 6B limitation is closed by an
explicit indeterminate state rather than by a stronger promise. The OS
boundary experiment may begin.
