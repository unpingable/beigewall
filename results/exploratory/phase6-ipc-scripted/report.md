# BLACKWALL process-separated run (phase 6, exploratory)

- label: scripted
- model: scripted-driver
- condition: control
- driver: scripted
- architecture: process-separated
- runs: executed [1, 2, 3], reused [], crashed []
- blackwall revision: a75709774b82f11b85f965fbe75b68b3166023eb

## Per run

| run | success | outcome classes | mean IPC latency ms | bytes/task | reconnects | transport failures | server exit |
|---|---|---|---|---|---|---|---|
| run-1 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.151 | 3813 | 0 | 0 | 0 |
| run-2 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.167 | 3813 | 0 | 0 | 0 |
| run-3 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.149 | 3813 | 0 | 0 | 0 |

Server exit code 0 means a clean SIGTERM shutdown; agent telemetry is advisory (self-reported across the boundary).
