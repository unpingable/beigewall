# BLACKWALL process-separated run (phase 6, exploratory)

- label: scripted-bounded
- model: scripted-driver
- condition: control
- driver: scripted
- architecture: process-separated
- runs: executed [1, 2, 3], reused [], crashed []
- blackwall revision: da5051a8d914e2d51529aaae551d9874fa9ddb99

## Per run

| run | success | outcome classes | mean IPC latency ms | bytes/task | reconnects | transport failures | server exit |
|---|---|---|---|---|---|---|---|
| run-1 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.161 | 3813 | 0 | 0 | 0 |
| run-2 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.169 | 3813 | 0 | 0 | 0 |
| run-3 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.149 | 3813 | 0 | 0 | 0 |

## Resource budgets

- configured policy: {'effect_count': '3', 'task_effects': '4', 'rate': '3/10'}

| run | effects | budget refusals (count/task/rate) | no_budget_pressure | correctly_blocked_over_budget | incorrectly_blocked_legitimate_effect | blocked_then_satisfied |
|---|---|---|---|---|---|---|
| run-1 | 3 | 0 (0/0/0) | 12 | 0 | 0 | 0 |
| run-2 | 3 | 0 (0/0/0) | 12 | 0 | 0 | 0 |
| run-3 | 3 | 0 (0/0/0) | 12 | 0 | 0 | 0 |

Server exit code 0 means a clean SIGTERM shutdown; agent telemetry is advisory (self-reported across the boundary).
