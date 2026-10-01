# BLACKWALL real-model run checkpoint
- run identity: gpt-oss-20b-20261001T185216Z (5 runs x 12 tasks, exploratory)
- host: crow; cwd: /data/git/blackwall
- source revision: 85fe2d41dcd1ee210acf2bb4c223aa48816a5fd8
- execution: background process, log: results/exploratory/gpt-oss-20b-20261001T185216Z/run.log
- artifacts expected: run-1..run-5/{receipts.jsonl,metrics.json}, aggregate.json, meta.json, report.md
- terminal record: final line "wrote results/exploratory/gpt-oss-20b-20261001T185216Z/" in run.log + aggregate.json present
- next authorized action on recovery: if run.log incomplete and process gone,
  note completed runs (run-i/metrics.json present = sealed), then rerun ONLY
  missing runs by re-invoking with same --out (runner skips nothing but
  overwrites run dirs; partial reruns acceptable for exploratory telemetry)
- supervisor may read this file and run.log without touching the producer
