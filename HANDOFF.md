# HANDOFF

Date: 2026-10-01. Status: **spike complete**. All milestones in
`docs/PLAN.md` are checked; the canonical experiment result is recorded.

## How to verify state

```
cd /data/git/blackwall
python3 -m unittest discover -s tests    # expect: Ran 106 tests ... OK
python3 run_experiment.py --out /tmp/bw-check
diff /tmp/bw-check/metrics.json results/canonical/metrics.json   # identical
diff /tmp/bw-check/receipts.jsonl results/canonical/receipts.jsonl
```

## Where things are

- `docs/PLAN.md` — durable plan, contracts, decisions, milestones (all done)
- `README.md` — architecture, threat model, known limitations, usage
- `FINDINGS.md` — canonical results and interpretation
- `results/canonical/` — authoritative receipts + metrics + report
- `blackwall/` — trusted layer: grants, receipts, corpus, projections,
  authority, broker, effects, tasks, narration, metrics, experiment
- `blackwall/agent/` — untrusted side: harness (tool loop + facade trap),
  drivers (scripted = normative; LLM = exploratory, env-gated)
- `tests/` — 106 unittest tests

## Resuming / extending

1. Read `docs/PLAN.md` first; do not change its contracts without updating it
   and `README.md`.
2. Work test-first: failing test, watch it fail, minimal implementation, watch
   it pass, keep `python3 -m unittest discover -s tests` green.
3. The scripted driver is normative. The LLM driver (`LLMDriver.from_env()`)
   must never change the receipt model, evaluation criteria, or authority
   semantics; it has been tested only against a stub server — a real-endpoint
   run is open follow-up work (see FINDINGS "Next steps").
4. New tasks go in `blackwall/tasks.py` with a check function and an intent
   note; update `docs/PLAN.md`'s table and expect metric totals in
   `tests/test_metrics.py` to change deliberately.

## Open items (none blocking)

- Real-endpoint LLM run (exploratory telemetry only).
- Strict-omission over-flagging (t10): consider query-scoped projections.
- OS-level authority boundary is explicitly out of spike scope.

## Notes

- Nested git repo: `/data/git` is an umbrella repo; BLACKWALL is its own repo
  inside it. Do not commit from the parent.
- Storage: repo ≈0.3 MB + results ≈59 KB; see FINDINGS "Storage closeout".
