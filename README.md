# BLACKWALL (spike)

A research spike testing one question:

> Can an agent remain useful when the computer it believes it controls is
> actually a mediated projection with no ambient authority?

The deliverable is an experimental result, not a production framework. All code
is throwaway-grade. There is deliberately **no** OS sandboxing: no namespaces,
containers, FUSE, seccomp, network policy, or real desktop integrations. The
spike validates the authority/provenance model in-process.

## Architecture

```
untrusted side                     trusted side (mediation layer)
┌───────────────────────┐          ┌──────────────────────────────────────┐
│ driver (scripted|LLM) │          │ authority   (policy, fail-closed)    │
│ harness tool loop     │  moves   │ broker      (sole tool surface)      │
│ ToolFacade ───────────┼─────────▶│  ├─ grants    scope/op/proj/ttl/budget│
│  (only request_grant, │          │  ├─ projections  disclosure boundary │
│   read, effect; every │          │  ├─ effects     send/create/update   │
│  other attribute is a │          │  └─ receipts    append-only ledger   │
│  trapped, receipted   │          │ corpus      (protected store)        │
│  unmediated attempt)  │          └──────────────────────────────────────┘
└───────────────────────┘
```

- **Capability broker** (`blackwall/broker.py`): the agent's only tool surface.
  Grants specify resource scope, operation, projection@version, expiration, and
  a cumulative usage budget. There is no permanent authorization state.
  Validation is fail-closed; every failure is a `refusal` receipt.
- **Projection engine** (`blackwall/projections.py`): the primary disclosure
  boundary. Each projection explicitly defines what leaves the protected store
  and returns provenance: schema@version, source object ids, disclosed field
  paths, content hash, and disclosure accounting. Provenance says
  "conversation@1 disclosed message.body from msg-1, msg-2, msg-3" — never
  "read thread alice".
- **Receipt system** (`blackwall/receipts.py`): every broker operation appends a
  durable receipt (grant_request / grant_mint / grant_deny / read / effect /
  refusal / unmediated_attempt / narrative / task_end). The receipt is
  authoritative; the agent's narration is merely another receipt, compared
  against the graph after the task.
- **Effect mediation** (`blackwall/effects.py`): reading never implies acting.
  Effects need their own grants and must carry dependency provenance
  (`derived_from`): projection refs must match content hashes actually
  disclosed to the task; user_input refs must match the registered prompt hash.
  Fabricated provenance is refused.
- **Receipt-grounded evaluator** (`blackwall/evaluator.py` with `facts.py`,
  `disclosure.py`, `claims.py`, `contracts.py`): scores a task from the
  receipt graph alone — authorized disclosure set → answer/effect claims →
  three separate verdicts (capability, admissibility, usefulness). The
  agent's narrative and the frozen expected answer are never consulted.
  Design and threat model: `docs/EVALUATOR.md`; results:
  `FINDINGS_EVALUATOR.md`.
- **Capability discovery** (`blackwall/capabilities.py`, phase 4): the
  broker's `discover` returns the *shape* of requestable authority for the
  task (applicable policy rules, projection and effect schemas, grant
  defaults, request schema), versioned and content-hashed, receipted as
  `discovery`. It grants nothing, shows no corpus value, and cannot confirm
  that a resource exists. The facade exposes it only in the treatment
  condition. See `docs/CAPABILITIES.md`, `FINDINGS_PHASE4.md`.
- **Agent harness** (`blackwall/agent/`): a simple tool loop. The scripted
  driver is the normative experiment (deterministic; intentionally includes
  correct behavior, overbroad requests, repeated grant requests, inference
  attempts on denied data, and inaccurate narration). The LLM driver is
  exploratory telemetry only — same interface, same boundary, same receipts;
  it changes nothing about evaluation.

## Threat model (spike scope)

Protected assets: the synthetic desktop corpus (messages, calendar, notes,
contacts) and the set of world-changing effects (send_message, create_event,
update_note).

The boundary under test: the agent (driver + harness-facing code) is untrusted;
the mediation layer is trusted. The spike exercises, as deterministic fixtures:

- requests beyond need (overbroad scope) → denied by authority policy
- requests for denied data → denied; subsequent *inference* of the datum shows
  up only as narrative overclaim, never as disclosure
- grant misuse: expired, wrong projection version, budget-exhausted,
  out-of-scope resource → refused at the broker
- effects with fabricated or missing provenance → refused
- capability attempts outside the broker surface (shell, file reads) → trapped,
  receipted, refused
- post-task narration that omits, overclaims, or stays vague → detected by
  diffing narrative against the receipt graph

Explicitly out of scope: OS-level enforcement. In this spike the boundary is by
construction (the driver never receives a store reference) within one Python
process. Nothing here stops a driver with interpreter-level access from
importing the corpus module. That is a documented limitation, not a property
claimed. See "Known limitations".

## Usage

```
python3 -m unittest discover -s tests     # 210 tests (1 expected failure)
python3 run_experiment.py                 # writes results/canonical/
python3 run_experiment.py --out /tmp/x    # byte-identical metrics/receipts
python3 run_reevaluation.py results/canonical                 # receipt-grounded
python3 run_reevaluation.py results/exploratory/<label>-<ts>  # re-scoring
# writes results/reevaluation/<basename>/{evaluation.json,report.md}
python3 run_llm_experiment.py --runs 5 --label <l> --condition treatment  # phase 4
python3 run_phase4_report.py <control_dir> <treatment_dir> --baseline <phase3_dir>
```

Canonical artifacts: `results/canonical/receipts.jsonl` (authoritative),
`metrics.json`, `report.md`. Interpretation: `FINDINGS.md`.

Exploratory real-model runs (opt-in, env-gated, never touch
`results/canonical/`):

```
BLACKWALL_LLM_BASE_URL=http://localhost:11434/v1 \
BLACKWALL_LLM_MODEL=gpt-oss:20b \
python3 run_llm_experiment.py --runs 5 --label gpt-oss-20b
# writes results/exploratory/<label>-<utc-ts>/
```

Exploratory artifacts and interpretation: `FINDINGS_REALMODEL.md`.

## Known limitations

- **Evaluation is deterministic claim extraction over a synthetic corpus,
  not general fact verification.** The receipt-grounded evaluator rejects
  answers that assert catalog facts, quantities, provenance tokens or
  effects the receipt graph does not support, and returns UNKNOWN for
  quoted claims it cannot place; an all-prose fabrication using none of
  those produces no claims and passes by absence of evidence (counted as
  `no_claims_extracted`). See `docs/EVALUATOR.md`.
- **The frozen string checks are a diagnostic, not the measurement.** They
  passed a fabricated t6 answer and required an undisclosed fact in t1.
- **The LLM driver captures `content` only.** Reasoning models that finish
  in a separate `reasoning` field without content produce an empty final
  answer (scored not useful) in every condition; ≈10–17% of executions in
  the gpt-oss:20b runs.
- **Capability discovery is a mechanism, not a usefulness fix.** In the
  phase 4 experiment it cut denied requests by more than half with every
  safety invariant intact, but converted none of the honest-but-useless
  executions and induced over-deference in one task (`FINDINGS_PHASE4.md`).

- **No OS isolation.** The authority boundary is convention, not mechanism.
  Realizing it needs process isolation or a capability-secure runtime.
- **Scripted normative driver.** It proves the mediation layer enforces and
  measures; it does not prove a real model would navigate grant requests as
  competently. The LLM adapter is stub-server tested, and one real-endpoint
  exploratory experiment has been run (local gpt-oss:20b, 5×12 tasks) — see
  `FINDINGS_REALMODEL.md`: the authority plane held in 60/60 executions while
  strict task success fell to 25%. One model, one endpoint; not a general
  result.
- **Dependency provenance, not taint.** `derived_from` records declared
  dependencies and validates their existence; it does not prove the effect's
  content actually flowed from them.
- **Strict omission counting.** The narrative comparator flags every disclosed
  object the narrative doesn't mention, including task-irrelevant ones
  (see FINDINGS).
- **Single-tenant authority.** One policy table, no revocation mid-task, no
  delegation chains.
- **Small corpus.** 12 task fixtures, ~25 corpus objects.
