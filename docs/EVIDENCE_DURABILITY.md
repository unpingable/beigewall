# Evidence durability (phase 6C)

Question: can accepted effects and authoritative receipts be recovered as
one coherent evidence history, including failures between
authorization, effect execution, budget consumption and receipt
persistence? Design first characterizes what the trusted process did
before this phase, then adds the smallest mechanism the observed failure
modes justify. Results: `FINDINGS_PHASE6C.md`.

## Four kinds of state, kept apart

| state | where it lives | who can read it |
|---|---|---|
| authoritative | the trusted process's memory: grants, consumption counters, task identity | the trusted process, now |
| durable evidence | the append-only receipt ledger on disk | the trusted process after a restart; any independent observer |
| external effect | the world the handler changed (in this spike an in-memory corpus: outbox, calendar, notes) | nobody but the handler; lost with the process here, permanent in a real deployment |
| indeterminate | an accepted effect whose outcome the trusted process cannot establish | recorded explicitly, never as success or failure |

The membrane's claim is about the second column: an independent observer
reconstructs every accepted use from receipts. Phase 6B's accepted
limitation (a receipt write failure after consumption leaves trusted
consumption ahead of durable evidence) broke that claim for one path.

## The effect path before this phase (characterized)

Per effect call in the trusted server, in order: resource mapping →
grant lookup → `validate_use` → provenance check → budget check →
**handler (world changes)** → budget `consume` → `grant.effects_used += 1`
→ `ledger.record("effect")` (in-memory append, then disk append, no
fsync) → response. Failure semantics at each point:

| failure | what was true afterwards |
|---|---|
| any failure before the handler | nothing changed; refusal receipted (or nothing, for a crash) — state 1 |
| crash during / after the handler, before the record | world changed, no receipt, no counter (crash loses memory) — **unrecorded** |
| disk write fails at the record | world changed, counter advanced, in-memory receipt present, nothing on disk; the client got an internal error — **misreported as failure** |
| crash during the write | a torn last line; the server refused to start — fail-closed, unrecoverable |
| response lost after completion | receipt exists; a retry on the same grant was refused "consumed" with no further information; a retry on a fresh grant landed a duplicate (phase 5C observed one) |
| client gone after completion | receipt exists; the consumed grant cannot be reused — fine |

Two of these violate the evidence claim (an effect with no evidence; an
effect reported as failed) and one invites duplication (a retry that
cannot learn the outcome).

## The mechanism (trusted server only)

The in-process broker — the frozen canonical reference — is unchanged:
`durable_effects=False`. The trusted server runs the broker with
`durable_effects=True`:

1. **Durable intent before the world changes.** After every check that
   can refuse, the broker mints `effect_id` (`e-N`, resumed across
   restarts like grant ids) and records `effect_intent {effect_id,
   grant_id, resource, effect {name, params}, derived_from}` with fsync.
   If that write fails the handler is not called: the response is error
   code `evidence` ("the effect did not occur") — state 1.
2. **Handler.** A handler failure records the existing refusal with the
   `effect_id` (durable): the intent is **aborted**; nothing was
   consumed; the grant remains usable — state 1.
3. **Consume, then commit.** Consumption and the grant's use counter
   advance, then `effect {…, effect_id}` is recorded with fsync — state 2.
   If the commit write fails the response is error code `indeterminate`
   with the effect_id and grant_id ("the effect may have occurred; do not
   retry on a fresh grant"), the grant stays consumed, and the server
   **fail-stops**: every later operation is the protocol error
   `ledger_failed` and `hello` reports `degraded` — a membrane that cannot
   write evidence does not act — state 3.
4. **Recovery on restart.** Every `effect_intent` with no commit and no
   abort is recorded as `effect_indeterminate {effect_id, grant_id,
   resource, effect, cause: unresolved_intent_at_restart, intent_receipt}`
   (durable). The resource rebuild counts committed and indeterminate
   intents as consumed and releases aborted ones. The task is closed by
   phase 6's rule anyway.
5. **Torn tail.** A crash during a write leaves an unparsable last line.
   The server refuses to start unless `--recover` is given; then it drops
   exactly that line, fsyncs, and records `trusted_restart
   {torn_tail_recovered: {bytes, sha256}}`. Any other corruption refuses
   to start regardless. If the torn line was a commit, its intent is
   unresolved and becomes indeterminate.
6. **Ledger ordering.** A row is visible in memory only after it reached
   the disk, and after one sink failure the ledger is failed for good
   (`LedgerError`). Only intent, commit, abort and indeterminate rows are
   fsynced; other rows may be lost by a crash and that loss is visible
   (seq gaps are not possible — the surviving prefix is contiguous — but
   the suffix is missing), never silently reinterpreted.

## Retry, replay, duplicates

- A retry **on the same grant** after a lost response is safe by
  construction: an effect grant is consumed by its first success, so the
  trusted side either performs the effect now (the first request never
  arrived) or refuses "effect grant consumed" and, new in this phase,
  names `prior_effect {effect_id, receipt_id, status: committed |
  indeterminate}` in the response. The refusal receipt is unchanged.
- The untrusted client (`RemoteBroker.effect`) does exactly that: on a
  transport failure it re-sends the identical effect on the same grant
  once; a committed prior effect becomes a recovered success observation
  (`recovered: true`, the original receipt id); an indeterminate one
  becomes an explicit indeterminate observation that says "do not retry".
  The facade never reports an indeterminate effect as failed.
- A **fresh grant** still lands a second effect if the policy approves it
  and the budget allows: evidence makes the duplicate distinguishable
  (two effect ids on two grants, both receipted) but does not prevent it.
  Prevention is the resource bound (phase 6B) and, beyond it, intent —
  the trusted side cannot know two differently-granted sends are "the
  same" send. Documented, not solved.
- A replayed effect frame (same grant) is refused; across tasks it is
  `task_not_active` / `unknown grant`; across a restart `task_closed` /
  `unknown grant`. Effect ids are never reused: the sequence resumes
  after the highest id in the prior ledger.
- A client-supplied `effect_id` is not in the effect op's argument
  whitelist: it is a protocol rejection before dispatch.

## What the observer can now establish

`lifecycle_metrics.reconstruct` reads intents, commits, aborts and
markers: an intent counts as accepted (and consumed) when written, is
released by an abort, and must be resolved by a commit, an abort or an
indeterminate marker. Disagreements: `commit_without_open_intent`,
`abort_without_open_intent`, `indeterminate_without_open_intent`,
`duplicate_effect_id`, `over_budget_intent` (an intent past the policy),
`intent_unresolved_after_restart` (an intent open at a restart that no
marker resolved). An intent still open at the end of a ledger is
`intent_pending_recovery` — the crash-in-flight state, reported, not a
disagreement. Legacy ledgers (no intents) are read as before.

## Cost

fsync on this host's disk is ≈25 ms; an effect pays two (intent and
commit). Non-effect calls are unchanged. Measured in `FINDINGS_PHASE6C.md`.

## Not claimed

- Transactional atomicity between the world and the ledger: the handler
  cannot be undone and the ledger cannot be written atomically with it.
  The claim is the three-state invariant above, nothing stronger.
- Durability of non-effect rows across a crash (not fsynced).
- Anything about a ledger an attacker can write: both processes are one
  UID (phase 6 limitation); phase 7 addresses that.
- Duplicate prevention across grants.
