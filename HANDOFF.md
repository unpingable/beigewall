# HANDOFF

Date: 2026-10-02. Status: **spike complete + real-model exploratory experiment
complete + receipt-grounded evaluator (phase 3) complete + capability
negotiation experiment (phase 4) complete + task-conditioned negotiation
(phase 5) complete + long-horizon authority lifecycle (phase 5B) complete +
context reset / forced expiry / refusal-oracle repair (phase 5C) complete**. All milestones in `docs/PLAN.md`
(phases 1–4) are checked. Phase 4 result: the discovery mechanism is sound
and halved negotiation overhead, but did **not** convert the
honest-but-useless class (0 conversions, 8 regressions); see
`FINDINGS_PHASE4.md`. The canonical experiment result is
recorded and unchanged; the real-model exploratory result is recorded in
`FINDINGS_REALMODEL.md`; the receipt-grounded re-scoring of both is in
`FINDINGS_EVALUATOR.md` with artifacts under `results/reevaluation/`.

## How to verify state

```
cd /data/git/blackwall
python3 -m unittest discover -s tests    # expect: Ran 272 tests ... OK
python3 run_experiment.py --out /tmp/bw-check
diff /tmp/bw-check/metrics.json results/canonical/metrics.json     # identical
diff /tmp/bw-check/receipts.jsonl results/canonical/receipts.jsonl # identical
diff /tmp/bw-check/report.md results/canonical/report.md           # identical
python3 run_reevaluation.py results/canonical    # 9/12 receipt-grounded, 0 unknown
```

## Where things are

- `docs/ECOSYSTEM-ROLE.md` — architectural positioning relative to the
  surrounding research (Constellation, Jev, ABSD, atproto-ACL, formal work)
- `docs/PLAN.md` — durable plan, contracts, decisions, milestones (phase 1 + 2,
  all done)
- `README.md` — architecture, threat model, known limitations, usage
- `FINDINGS.md` — canonical (normative) results and interpretation
- `FINDINGS_REALMODEL.md` — exploratory real-model results vs. baseline
- `results/canonical/` — authoritative receipts + metrics + report (normative)
- `results/exploratory/gpt-oss-20b-20261001T185916Z/` — sealed real-model
  experiment (5 runs × 12 tasks, aggregate + meta + report + checkpoint)
- `results/exploratory/gpt-oss-20b-20261001T185216Z/` — interrupted first
  attempt, retained as defect-discovery evidence (see FINDINGS_REALMODEL
  "Artifacts and storage disposition")
- `blackwall/` — trusted layer: grants, receipts, corpus, projections,
  authority, broker, effects, tasks, narration, metrics, experiment
- `docs/EVALUATOR.md` — receipt-grounded evaluator: formal structure,
  verdicts, threat model, limitations
- `FINDINGS_EVALUATOR.md` — old vs new scoring, t6 regression, gap
  decomposition, normative-surface verdict
- `blackwall/facts.py`, `disclosure.py`, `claims.py`, `contracts.py`,
  `evaluator.py` — the evaluator; `blackwall/reevaluation.py` +
  `run_reevaluation.py` — re-scoring of stored runs
- `results/reevaluation/{canonical,gpt-oss-20b-20261001T185916Z}/` —
  evaluation.json (every claim) + report.md; regenerable
- `docs/CAPABILITIES.md`, `FINDINGS_PHASE4.md` — phase 4: capability
  discovery design (what is exposed, what stays hidden) and the
  control-vs-treatment result
- `blackwall/capabilities.py`, `negotiation.py`, `phase4.py`,
  `run_phase4_report.py` — catalog, negotiation/abuse telemetry, comparison
- `results/exploratory/gpt-oss-20b-p4-control-*/`, `...-p4-treatment-*/` —
  phase 4 runs (5 × 12 each); `results/reevaluation/phase4-*/` — comparison
- `blackwall/llm_metrics.py`, `blackwall/llm_experiment.py`,
  `run_llm_experiment.py` — exploratory real-model telemetry, runner, CLI
- `blackwall/agent/` — untrusted side: harness (tool loop + facade trap),
  drivers (scripted = normative; LLM = exploratory, env-gated)
- `tests/` — 162 unittest tests (no real API key required; LLM paths use a
  stub server); `test_evaluator.py` holds the regression fixtures

## Resuming / extending

1. Read `docs/PLAN.md` first; do not change its contracts without updating it
   and `README.md`.
2. Work test-first: failing test, watch it fail, minimal implementation, watch
   it pass, keep `python3 -m unittest discover -s tests` green. After every
   change, re-verify `results/canonical/` byte-identical (commands above).
3. The scripted driver is normative. The LLM driver (`LLMDriver.from_env()`)
   must never change the receipt model, evaluation criteria, or authority
   semantics. Real-model runs are exploratory telemetry only, always written
   under `results/exploratory/<label>-<ts>/`, never `results/canonical/`.
4. To rerun the real-model experiment (needs a local OpenAI-compatible
   endpoint; no key needed for loopback):

   ```
   BLACKWALL_LLM_BASE_URL=http://localhost:11434/v1 \
   BLACKWALL_LLM_MODEL=gpt-oss:20b \
   python3 run_llm_experiment.py --runs 5 --label gpt-oss-20b
   ```

   The runner is resumable: sealed runs (those with `run-i/metrics.json`) are
   reused, missing runs re-executed into the same `--out` dir.
5. New tasks go in `blackwall/tasks.py` with a check function and an intent
   note, **and** an `AnswerContract` in `blackwall/contracts.py` (required /
   forbidden facts and effects, what the policy blocks); new corpus facts
   the task depends on go in the curated list in `blackwall/facts.py`.
   Update `docs/PLAN.md`'s table and expect metric totals in
   `tests/test_metrics.py` to change deliberately.
6. The receipt-grounded evaluator is the measurement surface; the frozen
   `Task.check` is a diagnostic. Never loosen a claim rule to raise a
   success rate; an uncertain claim returns UNKNOWN. Treat
   `no_claims_extracted > 0` in any run as a coverage alarm.

## Open items (none blocking)

- **Phase 5 sealed (2026-10-02)**: 4 conditions × 10 runs × 12 tasks under
  `results/exploratory/phase5-gpt-oss-20b/`; findings in
  `FINDINGS_PHASE5.md`. Headline: the task-scoped view converts 18/47
  honest-but-useless executions (p≈0.03, mostly by removing reasoning-only
  empty completions), t2/t4 never convert in 480 executions, and the task
  view induced 8 unrequested effects (t1 posted its summary into the
  thread). Global and denial effects are inside batch-to-batch drift.
- **Phase 5B sealed (2026-10-02)**: 7 probes + 20 real-model trajectories
  (130 task steps); every critical invariant zero; the observer
  reconstructed all 150 uses from receipts; no authority inflation.
  Defects fixed this phase: unbounded effect grants (duplicate on retry),
  forgeable move `op`, NaN TTL, unbounded requested budget. Agent findings:
  content bleeds across tasks (prior disclosure used as present authority
  to speak; 15 pure-bleed inadmissible executions), usefulness decays with
  session position (50% → ~20%), 40% empty answers. `FINDINGS_PHASE5B.md`.
- **Phase 5C sealed (2026-10-02)**: per-task context reset eliminated
  the pure cross-task bleed class (15 → 0) and the session decay (useful
  41 → 71 of 130) at 1.8× grant friction, trusted side unchanged; the
  forced mid-task expiry fired in 11/20 target tasks and the model never
  encountered it (it mints just in time and never reuses a grant), so
  real-model expiry recovery remains unobserved (deterministic coverage
  exists); refusal-reason oracle repaired. `FINDINGS_PHASE5C.md`.
- **Next**: the OS/process-boundary experiment. Not started here; start it
  deliberately. Carry forward: the budget-count wording re-seal; a
  lifecycle design where one grant must span several uses if real-model
  expiry recovery is to be observed; the "approvable ≠ expected" effect
  policy question.
- **Policy question raised by phase 5**: an approvable effect is not an
  expected effect; effect *grants* carry no user-intent reference.
- **Refusal-reason oracle (phase 5C): repaired for existence/type.**
  Unknown and wrong-typed ids share one reason on the read and effect
  paths; every refused read is charged against the grant's read budget;
  interface refusals keep their recovery information. Residual, bounded:
  the object-budget reason keeps its sealed wording (it names the object
  count of an in-scope resource) because changing it alters canonical t6
  receipts; close with a documented canonical re-seal. Tests:
  `tests/test_refusal_oracle.py`.

- **Rename before any public release.** "Blackwall" collides with CD Projekt
  Red's Cyberpunk term; the working suggestion is "beigewall" or similar.
  Do this before the repo, docs, or findings are published anywhere.
- Next research question (see FINDINGS_PHASE4): task-conditioned discovery
  (catalog returned with the first denial, or scoped by operation) to test
  whether the unconverted t2/t4 class is a planning gap rather than an
  information gap; and driver `reasoning`-field capture as telemetry
  (empty final answers are reasoning-only completions). OS/process
  isolation stays deferred until one of these converts the class with the
  invariants intact.
- New failure mode from phase 4: over-deference — the agent obtains the
  data, then answers with the policy instead of the task (t7, 3/5 treatment
  runs). Honest, admissible, useless.
- Evaluator coverage: all-prose fabrication (no catalog token, number,
  quote, email, date, provenance) is admissible by absence of evidence;
  tracked via `no_claims_extracted`.
- Strict-omission over-flagging (t10 narrative detector): consider
  query-scoped projections.
- Cross-model comparison is now meaningful (driver-independent evaluation).

## Notes

- Nested git repo: `/data/git` is an umbrella repo; BLACKWALL is its own repo
  inside it. Do not commit from the parent.
- Storage: repo + results ≈3 MB total (exploratory tree ≈1.8 MB incl. the
  two phase 4 batches, reevaluation ≈0.9 MB regenerable). The
  interrupted-attempt dir is retained pending disposition review; see
  FINDINGS_REALMODEL "Artifacts and storage disposition".
- Two broker fail-closed incompatibilities were found via the real model and
  fixed during this phase (unknown resource names; malformed `derived_from`).
  Both fixes were re-verified against canonical before use. Details:
  FINDINGS_REALMODEL "Concrete incompatibilities found and fixed".
