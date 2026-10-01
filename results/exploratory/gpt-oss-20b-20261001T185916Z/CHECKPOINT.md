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

## update 2026-10-01T19:15:31Z
- run-1, run-2 sealed (metrics.json present); run-3 interrupted by persistent
  endpoint HTTP 500 at ~13.6 min
- runner is now resumable: sealed runs are reused, missing runs re-executed
  into the same output dir; revision 18e622843c8afd128210d15909c44c2afe54ac4f
- next action: relaunch same command, same --out; expect run-3..run-5
