# FINDINGS — phase 5: task-conditioned capability negotiation

**Status: exploratory real-model evidence, scored by the receipt-grounded
evaluator.** Canonical results, every sealed earlier batch (phase 3 baseline,
phase 4 control and treatment), the corpus, tasks, policy, projections,
effects, receipt semantics and the evaluator are unchanged. Deterministic
claim extraction remains corpus-specific (`docs/EVALUATOR.md`);
receipt-grounded evaluation is authoritative for this experiment.

## Research question

Phase 4 showed that a *global* capability catalog is non-authorizing and
content-free, cuts denials and fabricated provenance, and does not convert
the honest/admissible/useless class (t2, t4 unchanged 10/10) while inducing
over-deference in t7. Phase 5 tests the narrower hypothesis:

> An agent may benefit from capability information when that information
> is presented at the point where the agent has established a concrete need
> for authority, rather than as a global catalog.

Not assumed. A clean null is a valid result.

## Conditions

All four share: model `gpt-oss:20b` on local Ollama (endpoint-default
sampling), the 12 tasks and their wording, `prompt_hash`, the policy table
(`policy_version 391354e7…`), projections, effects, receipt semantics, the
system-prompt environment manifest, `max_steps=40`, fresh corpus/ledger/
authority per run, the receipt-grounded evaluator, and 10 runs × 12 tasks
each. Only *when and how* capability information is exposed differs
(`docs/CAPABILITIES.md`, "Condition matrix"):

| condition | what the agent sees | when | tool | prompt delta |
|---|---|---|---|---|
| control | nothing beyond the manifest | — | — | none |
| global (A, phase 4 replication anchor) | full task-applicable catalog | on its own `discover()` call | +1 tool | one bullet |
| denial (B) | hint scoped to the denied resource class | attached to each grant denial / use refusal | none | one bullet |
| task (C) | view scoped to the classes the prompt grounds to | in the task message, before the first action | none | one bullet |

Every exposure is a `discovery` receipt with trigger, scope, policy version
and content hash. No exposure issues a grant; every request still passes
the unchanged authority policy. The hint for a nonexistent id is identical
to the hint for a real one except for the echoed request; the task view
names no object. Both asserted in tests.

Task grounding for condition C is a fixed lexicon plus a generic
person-name rule (table in `docs/CAPABILITIES.md`). It is the same
mechanism for every task, but it was written knowing the task wording — a
limitation, stated below.

## Measurement

Each execution is evaluated from receipts (`evaluate_task`) and classed
as useful / honest+admissible+useless / inadmissible / capability failure /
unknown (phase 4 definitions). Primary: per-task and per-condition counts
of the useless class against the fresh phase 5 control, with per-run
variance. Secondary (per condition, mean and variance where per-run):
grants requested, minted, denied, minted/requested, broker calls,
refusals, denial recovery, request selection, disclosure surface,
unsupported claims, fabricated-provenance refusals, unmediated attempts,
narrative divergence, empty answers, reasoning-only completions (driver
metadata: presence/size of a non-answer field, never its content).

Over-deference diagnostic (`blackwall/deference.py`), per execution from
receipts plus the evaluation, never from wording alone: blocked correctly /
useful from authorized information / policy explanation replacing a useful
answer (required facts *disclosed* but absent from the answer, which talks
policy) / unnecessary refusal with authority (a grant covering a required
class minted but never used; answer refuses) / empty / other. Unnecessary
authority requests: requests after sufficiency (every required item already
authorized) and requests outside the task's expected scope.

Acquisition timing: for each minted grant, the trigger of the most recent
discovery receipt before it (initial / denial / global / none) and whether
that discovery's scope covered the minted class.

## Adversarial review before the matrix

Summarised in `docs/CAPABILITIES.md` "Adversarial review disposition".
Two findings matter for interpreting results:

1. **Open defect (pre-existing, not fixed this phase): refusal reasons are
   an existence/type/count oracle.** With any read grant, probing
   `messages/thread/thr-nobody` returns "unknown resource", `notes/evt-2`
   returns "evt-2 is not a note", and an undersized budget returns
   "13 > 1", at no budget cost. Present in every condition and every
   earlier batch. Pinned by `TestKnownOpenDefect` (expected failure).
   Mitigations this phase: the evaluator no longer treats refusal text as
   authorized for fact claims (an id learned by probing is unsupported),
   and the denial hint no longer repeats the reason. The telemetry below
   counts refusals whose reason names an object the task never disclosed
   as **probe-shaped refusals**.
2. **The evaluator change is uniform and did not move any stored verdict**
   (phase 3 baseline and canonical re-evaluated: identical counts).

RESULTS_PLACEHOLDER
