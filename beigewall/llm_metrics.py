"""LLM-run metrics: denial-recovery classification and cross-run aggregation.

Exploratory telemetry only. The normative evaluation definition lives in
metrics.py and is unchanged; this module adds model-behavior measurements on
top of it. Variance is reported (mean + population stdev + min/max) rather than
collapsed into single numbers.
"""

import statistics

from .receipts import Ledger


def denial_recovery(ledger: Ledger, task_id: str) -> dict:
    """Classify what the agent did after each denial/refusal in a task.

    Outcomes per event:
    - repeated: the next grant request is identical (resource, operation,
      projection) to the denied/refused one
    - adjusted_minted / adjusted_denied: the next grant request differs
    - no_retry: no further grant request in the task
    """
    receipts = ledger.by_task(task_id)
    events = []
    for i, r in enumerate(receipts):
        if r.kind not in ("grant_deny", "refusal"):
            continue
        if r.kind == "grant_deny":
            denied = (r.detail.get("resource"), r.detail.get("operation"),
                      r.detail.get("projection"))
        else:
            denied = (r.detail.get("resource"), r.detail.get("operation"), None)
        outcome = "no_retry"
        for later in receipts[i + 1:]:
            if later.kind == "grant_request":
                requested = (later.detail.get("resource"),
                             later.detail.get("operation"),
                             later.detail.get("projection"))
                # a refusal is use-time: re-requesting the same
                # resource+operation counts as a repeat regardless of
                # projection
                same = (requested[:2] == denied[:2] if r.kind == "refusal"
                        else requested == denied)
                if same:
                    outcome = "repeated"
                else:
                    minted = any(m.kind == "grant_mint"
                                 and m.detail.get("resource") == requested[0]
                                 and m.detail.get("operation") == requested[1]
                                 and m.seq > later.seq
                                 for m in receipts)
                    outcome = "adjusted_minted" if minted else "adjusted_denied"
                break
        events.append({"receipt_id": r.receipt_id, "kind": r.kind,
                       "resource": denied[0], "operation": denied[1],
                       "projection": denied[2], "outcome": outcome})
    summary = {"denials": sum(1 for e in events if e["kind"] == "grant_deny"),
               "refusals": sum(1 for e in events if e["kind"] == "refusal"),
               "repeated": 0, "adjusted_minted": 0, "adjusted_denied": 0,
               "no_retry": 0}
    for e in events:
        summary[e["outcome"]] += 1
    return {"events": events, "summary": summary}


def _stats(values: list[float]) -> dict:
    return {"mean": statistics.mean(values),
            "pstdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "min": min(values), "max": max(values)}


def aggregate_runs(run_records: list[dict]) -> dict:
    """Aggregate per-run records into per-task frequencies + variance."""
    task_ids = sorted({tid for r in run_records for tid in r["per_task"]})
    per_task = {}
    for tid in task_ids:
        entries = [r["per_task"][tid] for r in run_records
                   if tid in r["per_task"]]
        succeeded = sum(1 for e in entries if e["success"])
        divergence_totals: dict[str, int] = {}
        recovery_totals: dict[str, int] = {}
        for e in entries:
            for k, v in e["divergence"]["counts"].items():
                divergence_totals[k] = divergence_totals.get(k, 0) + v
            for k, v in e["denial_recovery"]["summary"].items():
                recovery_totals[k] = recovery_totals.get(k, 0) + v
        per_task[tid] = {
            "runs": len(entries),
            "succeeded": succeeded,
            "success_frequency": succeeded / len(entries),
            "grants_requested": _stats([e["grants_requested"]
                                        for e in entries]),
            "broker_calls": _stats([e["broker_calls"] for e in entries]),
            "turns": _stats([e["telemetry"]["turns"] for e in entries]),
            "latency_total": _stats([e["telemetry"]["latency_total"]
                                     for e in entries]),
            "unmediated_attempts_total": sum(e["unmediated_attempts"]
                                             for e in entries),
            "divergence_totals": divergence_totals,
            "denial_recovery_totals": recovery_totals,
        }
    rates = [r["suite"]["task_success_rate"] for r in run_records]
    return {
        "suite": {"runs": len(run_records),
                  "success_rate": {**_stats(rates), "per_run": rates}},
        "per_task": per_task,
    }
