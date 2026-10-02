# Resource budgets: usefulness comparison

- **control**: `/data/git/blackwall/results/exploratory/phase6-ipc-gpt-oss-20b` condition `control`; 5 sealed runs, 60 executions; policy None
- **bounded**: `/data/git/blackwall/results/exploratory/phase6b-ipc-gpt-oss-20b-bounded` condition `control`; 5 sealed runs, 60 executions; policy {'effect_count': 3, 'task_effects': 4, 'rate': {'n': 3, 'seconds': 10.0}}

## Outcome classes (receipt-grounded)

| measure | control | bounded |
|---|---|---|
| inadmissible | 2 | 6 |
| unknown | 1 | 0 |
| useful | 33 | 33 |
| useless | 24 | 21 |
| executions | 60 | 60 |

## Budget classification (task-runs)

| measure | control | bounded |
|---|---|---|
| unknown_need | 0 | 0 |
| no_budget_pressure | 60 | 60 |
| correctly_blocked_over_budget | 0 | 0 |
| incorrectly_blocked_legitimate_effect | 0 | 0 |
| blocked_then_satisfied | 0 | 0 |

## Effects and budget refusals (totals)

| measure | control | bounded |
|---|---|---|
| effects | 8 | 10 |
| budget refusals | 0 | 0 |
|   count | 0 | 0 |
|   task | 0 | 0 |
|   rate | 0 | 0 |
| unrequested effects | 0 | 1 |
| duplicate effects | 0 | 0 |
| missing expected effects | 9 | 9 |

## Grants

| measure | control | bounded |
|---|---|---|
| grants requested | 230 | 201 |
| grants minted | 82 | 74 |
| grants per successful task | 6.97 | 6.09 |
| grant churn | 14 | 1 |

## Per-run distributions (mean, sd, min..max)

| measure | control | bounded |
|---|---|---|
| effects | 1.60 (sd 0.49, 1.00..2.00) | 2.00 (sd 0.89, 1.00..3.00) |
| budget refusals | 0.00 (sd 0.00, 0.00..0.00) | 0.00 (sd 0.00, 0.00..0.00) |
| grants requested | 46.00 (sd 6.69, 38.00..58.00) | 40.20 (sd 5.46, 32.00..47.00) |
