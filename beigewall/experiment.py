"""Experiment runner: executes the fixed suite and writes durable artifacts.

Artifacts per run directory:
- receipts.jsonl  (authoritative append-only ledger)
- metrics.json    (machine-readable results)
- report.md       (human-readable summary)

Runs are deterministic (logical clock, sequential ids): two runs produce
byte-identical receipts.jsonl and metrics.json.
"""

import json
import os

from .metrics import suite_metrics, task_metrics
from .tasks import run_suite


def run_experiment(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    receipts_path = os.path.join(out_dir, "receipts.jsonl")
    if os.path.exists(receipts_path):
        os.unlink(receipts_path)  # rerun overwrites; ledger is rebuilt from scratch
    outcomes, corpus, ledger = run_suite(sink_path=receipts_path)

    per_task = {t.task_id: task_metrics(ledger, t.task_id, s)
                for t, r, s in outcomes}
    success = {t.task_id: s for t, r, s in outcomes}
    answers = {t.task_id: r.answer for t, r, s in outcomes}
    suite = suite_metrics(per_task, success)

    metrics = {"suite": suite, "per_task": {tid: per_task[tid]
                                            for tid in sorted(per_task)}}
    with open(os.path.join(out_dir, "metrics.json"), "w") as fh:
        json.dump(metrics, fh, indent=2, sort_keys=True)
        fh.write("\n")

    with open(os.path.join(out_dir, "report.md"), "w") as fh:
        fh.write(_render_report(suite, per_task, answers))
    return metrics


def _render_report(suite: dict, per_task: dict, answers: dict) -> str:
    lines = [
        # FROZEN historical title: the canonical report was sealed under the
        # project's former name (BLACKWALL). Kept verbatim so run_experiment.py
        # regenerates the canonical artifact byte-identically after the rename
        # to BeigeWall (see RENAME.md). Do not change this string.
        "# BLACKWALL experiment report",
        "",
        "## Headline metrics",
        "",
        f"- task_success_rate: {suite['task_success_rate']:.3f} "
        f"({suite['tasks_succeeded']}/{suite['tasks_total']})",
        f"- tasks_failed: {', '.join(suite['tasks_failed']) or 'none'}",
        f"- grants_requested_total: {suite['grants_requested_total']}",
        f"- authorization_events_total: {suite['authorization_events_total']}",
        f"- grants_minted_total: {suite['grants_minted_total']}",
        f"- grants_denied_total: {suite['grants_denied_total']}",
        f"- broker_calls_total: {suite['broker_calls_total']}",
        f"- reads_disclosed_total: {suite['reads_disclosed_total']}",
        f"- effects_performed_total: {suite['effects_performed_total']}",
        f"- refused_operations_total: {suite['refused_operations_total']}",
        f"- unmediated_attempts_total: {suite['unmediated_attempts_total']}",
        f"- grants_requested_per_successful_task: "
        f"{suite['grants_requested_per_successful_task']:.2f}",
        f"- usefulness_under_denial: "
        f"{suite['usefulness_under_denial']['succeeded_despite_denial']} of "
        f"{suite['usefulness_under_denial']['tasks_facing_denial']} tasks that "
        f"faced a denial/refusal still succeeded",
        "",
        "## Disclosure surface",
        "",
        f"- distinct fields disclosed: "
        f"{len(suite['disclosure_surface_total']['distinct_fields'])}",
        f"- objects disclosed (sum over tasks): "
        f"{suite['disclosure_surface_total']['objects_disclosed']}",
        f"- bytes disclosed: "
        f"{suite['disclosure_surface_total']['bytes_disclosed']}",
        "",
        "## Narrative vs receipt divergence",
        "",
    ]
    for k, v in suite["narrative_divergence_totals"].items():
        lines.append(f"- {k}: {v}")
    lines += ["",
              f"- tasks with divergence: "
              f"{', '.join(suite['tasks_with_divergence']) or 'none'}",
              "",
              "## Per task",
              ""]
    for tid, m in sorted(per_task.items()):
        lines.append(f"### {tid} — {'success' if m['success'] else 'FAILED'}")
        lines.append("")
        lines.append(f"- grants: {m['grants_requested']} requested, "
                     f"{m['grants_minted']} minted, {m['grants_denied']} denied")
        lines.append(f"- broker calls: {m['broker_calls']}; "
                     f"reads disclosed: {m['reads_disclosed']}; "
                     f"effects: {m['effects_performed']}; "
                     f"refusals: {m['refused_operations']}; "
                     f"unmediated attempts: {m['unmediated_attempts']}")
        div = {k: v for k, v in m["divergence"]["counts"].items() if v}
        lines.append(f"- divergence: {div or 'none'}")
        if m["denial_reasons"]:
            lines.append(f"- denials: {m['denial_reasons']}")
        if m["refusal_reasons"]:
            lines.append(f"- refusal reasons: {m['refusal_reasons']}")
        lines.append(f"- answer: {answers[tid]}")
        lines.append("")
    return "\n".join(lines)
