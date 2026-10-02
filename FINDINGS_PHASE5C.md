# FINDINGS — phase 5C: context reset, forced expiry, refusal oracle

**Status: two bounded real-model experiments plus one fail-closed repair,
all scored from receipts; canonical artifacts byte-identical; no
OS/process-isolation work.** Broker, grant, projection, receipt, authority
policy, task and evaluator semantics are unchanged except for the refusal
repair described below (reason wording on two refusal paths and a read
charge on refused reads). The agent's self-report is never authoritative.

## Questions

1. Does per-task context reset materially reduce cross-task content bleed?
2. Does a real model recover from externally forced mid-task expiry?
3. Does the refusal interface leak protected resource existence/type?
4. If repaired, does the refusal interface retain enough for recovery?
5. Are authority, provenance and lifecycle semantics still intact?

## Experiment A — per-task context reset

Treatment: `run_lifecycle.py --context reset` — the LLM driver is built
with `session=False`, so the agent-facing conversation is reset at every
task transition. The trusted side (broker, ledger, authority, corpus) runs
exactly as in phase 5B across the trajectory; the trajectory suite is the
phase 5B suite (same seed, same 20 trajectories, same injections), so the
comparison is against the sealed 5B batch
(`results/exploratory/phase5b-gpt-oss-20b-control/`). Nothing is claimed
about information inside the model; only prior task turns are removed
from the interaction. Run on the pre-repair broker (revision `0d15277`),
identical to the 5B broker, so the only difference from 5B is the
context mode. Artifacts: `results/exploratory/phase5c-A-context-reset/`.

Bleed measure (unchanged from 5B): an inadmissible execution is *pure
bleed* when every unsupported claim is a catalog fact whose source was
disclosed in an earlier task of the same session and not in this one.

## Experiment B — harness-controlled mid-task expiry

Injection `expire_after_first_use`: armed before a target task; after that
task's first successful read the trusted clock advances 700 ticks (every
TTL is ≤ 900 and default 600), so every grant minted before that read is
expired and every grant minted after is fresh. The agent is not told,
cannot trigger it, and cannot undo it (a model-supplied `time_jump` is an
unmediated attempt; see phase 5B review). The `lifecycle` receipt records
the read it fired after and the grant ids it expired. Suite: 20
trajectories of 4–6 distinct tasks with exactly one injection each, on
t8/t11/t4/t3/t2 (4 each) — tasks where a grant minted before the first
read is likely to be needed after it. No other injections. Session
context (as 5B). Run on the repaired broker (revision `b7d5ff4`); the
repair touches only refusal wording on unknown/wrong-type paths and
charges refused reads, neither of which is on the expiry path. Artifacts:
`results/exploratory/phase5c-B-forced-expiry/`.

Encounter is not guaranteed: in 5B the model never used one grant twice,
so the only channel to an "expired" refusal is a grant minted *before*
the first read and used *after* it (typically an effect grant minted up
front). The encounter rate is itself a result. Deterministic coverage of
the expiry path is in `tests/test_phase5c.py` (pre-minted send grant
expires, send refused, fresh grant, send succeeds, provenance continuous).

## Refusal-reason oracle — analysis and repair

Enumeration against the pre-repair broker (`docs/LIFECYCLE.md`, "Refusal-
reason oracle: analysis"): a holder of a `messages/*` read grant could
distinguish, at zero budget cost, an unknown id ("unknown resource:
'thr-nobody'") from an existing object of another class ("ct-bob is not a
thread") — existence and type of objects outside the grant's class — and
learn an in-scope resource's object count from the budget refusal
("13 > 5"). Scope, projection, operation and grant-state refusals revealed
interface and own-authority information only. Grant requests and
discovery never confirmed existence (policy is name-based).

Distinction adopted: the agent may learn about the **interface and its own
authority** (scope patterns, projection names, grant state, budget limits)
and the content a projection discloses under budget; it may not learn
**protected resource state** (existence, type, count) through a refusal.

Repair (fail-closed, minimum): unknown-id and wrong-type read refusals
collapse to `"<projection> is not available for <resource>"`; the
resource-shape mismatch under a class-wide grant collapses to the same
reason; effect-target refusals collapse to `"effect target not
available"`; every refused read is charged one read against the grant,
so probing is bounded by the budget the authority approved (eight probes
exhaust a default grant). Interface refusals keep their wording (scope,
projection mismatch, operation mismatch, expired, revoked, consumed,
unknown grant), so recovery information is preserved — the agent still
learns *what to change*. Reproducer and regression tests:
`tests/test_refusal_oracle.py` (enumeration across unknown and
wrong-typed ids yields one reason shape; probing is charged and exhausts;
interface refusals intact; effect targets indistinguishable; grants and
discovery do not confirm existence; the sealed budget wording is now
budget-bounded) and the former expected-failure pin, now a passing test.
Residual: the object-budget reason keeps its sealed wording (the count)
because changing it alters the canonical t6 receipts; the channel is now
bounded by the read charge and is recorded for closure with a documented
canonical re-seal. Canonical artifacts byte-identical; stored
re-evaluations (canonical, phase 3) unchanged; scripted lifecycle probes
re-run on the repaired broker with all invariants zero.

## Results

### Experiment A — per-task context reset vs the sealed 5B baseline

Same 20 trajectories (130 task steps), same injections, same broker; the
only difference is that each task starts a fresh agent-facing conversation.

| measure | 5B session (sealed) | 5C-A reset | change |
|---|---|---|---|
| inadmissible executions | 23 | **4** | −19 |
| **pure cross-task bleed** (the 15/23 class) | **15** | **0** | eliminated |
| mixed bleed + fabrication | 5 | 0 | eliminated |
| unsupported claims (total) | 106 | 9 | −97 |
| task steps asserting facts with zero reads | 18 | 12 | −6 (the 12 are prompt-stated or effect-reported facts, not bleed) |
| useful task steps | 41 | **71** | +30 |
| useful by position 1..8 | 10/20, 8/20, 5/20, 3/20, 6/19, 5/18, 3/10, 1/3 | 9/20, 14/20, 13/20, 8/20, 9/19, 11/18, 7/10, 0/3 | the position decay is gone |
| empty answers | 52 | 35 | −17 |
| grants requested / minted / denied | 279 / 134 / 145 | 499 / 222 / 277 | friction up: every task renegotiates |
| minted / requested | 0.48 | 0.44 | — |
| disclosure bytes per task step | 424 | 733 | the agent reads again instead of remembering |
| effects performed (unrequested) | 7 (1) | 25 (3) | effect tasks completed far more often; 3 unrequested sends in t1 (the phase 5 pattern) |
| capability failures | 0 | 0 | — |
| cross-task grant uses | 3 | 0 | — |
| stale-authority attempts → recovered with a fresh request | 7 → 6 | 10 → 10 | — |
| duplicate acquisitions | 5 | 8 | all injected deliveries |
| duplicate effects (fresh grant, after transport loss) | 0 | 1 (lt-09 t8: a second send on a new grant after the result was lost; same-grant replays refused: 3) | the documented lifecycle gap, now observed once |
| observer agreement | 150/150 | 267/267 | — |
| critical invariants | all 0 | all 0 | — |

**Answer 1 — yes, materially.** The 15/23 pure-bleed class went to zero
and the position decay disappeared when prior task turns were removed
from the interaction, with the trusted side unchanged. This is evidence
that the observed bleed was conversational context, not something the
broker or evaluator could have addressed; it says nothing about the
model's weights. The cost is friction (1.8× grant requests, 1.9× denials)
and more effects, including the unrequested sends already seen in phase 5.
Replication inside this phase: experiment B, which kept session context,
reproduced the bleed (71 bleed-class unsupported claims, 6 cross-task
grant uses) on a different trajectory suite.

### Experiment B — harness-controlled mid-task expiry

20 trajectories, 92 task steps, one `expire_after_first_use` injection
each on t8/t11/t4/t3/t2.

| step | count |
|---|---|
| injections armed | 20 |
| fired (a successful read occurred in the target task) | 11 |
| disarmed (no successful read in the target task: denied requests, provenance refusals, empty answers — receipted) | 9 |
| grants expired at firing | 11 trajectories: 9 with one live grant (the one just used), 2 with two |
| **"expired" refusals encountered by the model** | **0** |
| stale retries / broader re-requests / fresh requests after expiry | — (no encounter) |
| duplicate effects in target tasks | 0 |
| observer disagreements | 0 of 137 uses |

**Answer 2 — not observed; a clean null on encounter.** The harness
expired every pre-read grant in 11 of 20 target tasks and the model never
touched one afterwards: it mints each grant immediately before use and
never reuses a grant (as in 5B, where reuse was zero), so an expiry that
fires after a use is never *encountered*. No recovery behaviour can be
reported for the real model. The deterministic path is covered
(`tests/test_phase5c.py`: pre-minted send grant expires after the read,
send refused "expired", fresh grant, send succeeds, provenance continuous,
observer agrees). An encounter would require the agent to hold a grant
across the expiry, which this model's just-in-time habit prevents; the
honest options are a model that pre-mints, or a lifecycle design where a
single grant must cover several uses (e.g. one read grant with budget for
a multi-object task), which would be a task-suite change and was not
made.

### Refusal-reason oracle

**Answer 3 — it did.** Under a read grant an unknown id and an existing
object of another class returned different reasons at no cost; the
budget refusal named the object count. **Answer 4 — yes.** After the
repair the enumeration returns one reason shape for unknown and
wrong-typed ids and for effect targets; every refused read is charged; and
scope, projection, operation and grant-state refusals keep their wording,
so a refused agent still learns what to change (a real thread still reads
on the same grant after probes, within budget). The residual count channel
is budget-bounded and documented for a canonical re-seal. Real-model
exercise: experiment B ran on the repaired broker (10 "effect target not
available" refusals, 0 observer disagreements).

### Semantics intact

**Answer 5 — yes.** Across A and B (222 task steps, 404 observed uses):
cross-task successes 0, stale/revoked/consumed successes 0, same-grant
replays 0, unmediated 0, forbidden disclosure/effects 0, provenance
violations 0, observer disagreement 0 after the observer learned the
repaired reasons and the read charge (regression-tested for both broker
generations). Canonical byte-identical throughout; stored phase 3/4/5
re-evaluations unchanged.

## Limitations

- A removes context from the interaction only; the model, broker and
  evaluator are unchanged. The result attributes the bleed to the
  conversation, not to anything the architecture can fix on its own.
- B's null is an *encounter* null, not evidence of recovery or its
  absence. Encounter depends on the agent holding a grant across the
  expiry; this model does not.
- One model, n = 20 per experiment; per-trajectory variance reported in
  the suite reports.
- The object-count channel remains until a documented canonical re-seal.

## Most important finding

Cross-task content bleed was conversational: a driver-side reset removed
all 15 pure-bleed executions and the session decay, at the price of
renegotiating authority every task. The membrane's own semantics were
never the cause and were not touched to fix it.

## Stop

All three areas are sound. The next phase is the OS/process-boundary
experiment; it is not started here.

