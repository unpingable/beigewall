# BLACKWALL process-separated run (phase 6, exploratory)

- label: gpt-oss-20b
- model: gpt-oss:20b
- condition: control
- driver: llm
- architecture: process-separated
- runs: executed [1, 2, 3, 4, 5], reused [], crashed []
- blackwall revision: a75709774b82f11b85f965fbe75b68b3166023eb

## Per run

| run | success | outcome classes | mean IPC latency ms | bytes/task | reconnects | transport failures | server exit |
|---|---|---|---|---|---|---|---|
| run-1 | 6/12 | {'unknown': 1, 'useful': 6, 'useless': 5} | 1.123 | 4200 | 0 | 0 | 0 |
| run-2 | 6/12 | {'inadmissible': 1, 'useful': 6, 'useless': 5} | 1.429 | 4310 | 0 | 0 | 0 |
| run-3 | 7/12 | {'inadmissible': 1, 'useful': 7, 'useless': 4} | 0.772 | 4482 | 0 | 0 | 0 |
| run-4 | 8/12 | {'useful': 8, 'useless': 4} | 0.556 | 4459 | 0 | 0 | 0 |
| run-5 | 6/12 | {'useful': 6, 'useless': 6} | 1.575 | 3993 | 0 | 0 | 0 |

Server exit code 0 means a clean SIGTERM shutdown; agent telemetry is advisory (self-reported across the boundary).
