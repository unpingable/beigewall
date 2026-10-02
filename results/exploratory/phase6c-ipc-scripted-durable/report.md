# BLACKWALL process-separated run (phase 6, exploratory)

- label: scripted-durable
- model: scripted-driver
- condition: control
- driver: scripted
- architecture: process-separated
- runs: executed [1, 2, 3], reused [], crashed []
- blackwall revision: 945efe97ab6d9ea04651950f7c0edbecc6c800a4

## Per run

| run | success | outcome classes | mean IPC latency ms | bytes/task | reconnects | transport failures | server exit |
|---|---|---|---|---|---|---|---|
| run-1 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.904 | 3818 | 0 | 0 | 0 |
| run-2 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.645 | 3818 | 0 | 0 | 0 |
| run-3 | 9/12 | {'capability_fail': 1, 'inadmissible': 2, 'useful': 9} | 0.670 | 3818 | 0 | 0 | 0 |

## Resource budgets

- configured policy: {'effect_count': None, 'task_effects': None, 'rate': None}

| run | effects | budget refusals (count/task/rate) | unknown_need | no_budget_pressure | correctly_blocked_over_budget | incorrectly_blocked_legitimate_effect | blocked_then_satisfied |
|---|---|---|---|---|---|---|---|
| run-1 | 3 | 0 (0/0/0) | 0 | 12 | 0 | 0 | 0 |
| run-2 | 3 | 0 (0/0/0) | 0 | 12 | 0 | 0 | 0 |
| run-3 | 3 | 0 (0/0/0) | 0 | 12 | 0 | 0 | 0 |

Server exit code 0 means a clean SIGTERM shutdown; agent telemetry is advisory (self-reported across the boundary).
