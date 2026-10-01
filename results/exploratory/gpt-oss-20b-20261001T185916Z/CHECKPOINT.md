# BLACKWALL real-model run checkpoint (second attempt)
- run identity: gpt-oss-20b-20261001T185916Z (5 runs x 12 tasks, exploratory)
- supersedes: gpt-oss-20b-20261001T185216Z (crashed at 51s on the effect-arg
  mapping defect; that defect and a malformed-derived_from defect are since
  fixed and committed; first dir retained as discovery evidence until closeout)
- host: crow; cwd: /data/git/blackwall
- source revision: edf24b20431302766c33a603321d5017693eff59
- execution: background process, log: results/exploratory/gpt-oss-20b-20261001T185916Z/run.log
- artifacts expected: run-1..run-5/{receipts.jsonl,metrics.json},
  aggregate.json, meta.json, report.md
- terminal record: "wrote results/exploratory/gpt-oss-20b-20261001T185916Z/" as last line of run.log
- recovery: a run-i is complete iff run-i/metrics.json exists; a fresh
  supervisor may rerun missing runs in a NEW output dir and note the split
