# HANDOFF

Date: 2026-10-01. Status: **spike complete + real-model exploratory experiment
complete**. All milestones in `docs/PLAN.md` (phase 1 and phase 2) are checked.
The canonical experiment result is recorded and unchanged; the real-model
exploratory result is recorded in `FINDINGS_REALMODEL.md`.

## How to verify state

```
cd /data/git/blackwall
python3 -m unittest discover -s tests    # expect: Ran 134 tests ... OK
python3 run_experiment.py --out /tmp/bw-check
diff /tmp/bw-check/metrics.json results/canonical/metrics.json     # identical
diff /tmp/bw-check/receipts.jsonl results/canonical/receipts.jsonl # identical
diff /tmp/bw-check/report.md results/canonical/report.md           # identical
```

## Where things are

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
- `blackwall/llm_metrics.py`, `blackwall/llm_experiment.py`,
  `run_llm_experiment.py` — exploratory real-model telemetry, runner, CLI
- `blackwall/agent/` — untrusted side: harness (tool loop + facade trap),
  drivers (scripted = normative; LLM = exploratory, env-gated)
- `tests/` — 134 unittest tests (no real API key required; LLM paths use a
  stub server)

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
   note; update `docs/PLAN.md`'s table and expect metric totals in
   `tests/test_metrics.py` to change deliberately.

## Open items (none blocking)

- Receipt-grounded outcome evaluation (answer-vs-disclosure consistency) —
  the recommended next research question; see FINDINGS_REALMODEL. Motivated
  by t6 run-2: a fabricated answer passed the frozen string check while
  receipts proved zero disclosure.
- Strict-omission over-flagging (t10): consider query-scoped projections.
- OS-level authority boundary: the experiment AFTER evaluation hardening;
  explicitly out of scope so far.
- Cross-model comparison is premature until evaluation is driver-independent.

## Notes

- Nested git repo: `/data/git` is an umbrella repo; BLACKWALL is its own repo
  inside it. Do not commit from the parent.
- Storage: repo + results ≈0.9 MB total (exploratory tree ≈576 KB). The
  interrupted-attempt dir is retained pending disposition review; see
  FINDINGS_REALMODEL "Artifacts and storage disposition".
- Two broker fail-closed incompatibilities were found via the real model and
  fixed during this phase (unknown resource names; malformed `derived_from`).
  Both fixes were re-verified against canonical before use. Details:
  FINDINGS_REALMODEL "Concrete incompatibilities found and fixed".
