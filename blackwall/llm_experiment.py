"""LLM experiment runner: the fixed task suite under a real-model driver.

Exploratory telemetry only. Canonical results are never touched: each run gets
its own directory under an explicit exploratory output root, and the normative
evaluation code path (run_experiment / metrics definitions) is unchanged.

Per-run isolation: fresh corpus, ledger, authority, broker, and driver per run.
Task order and policy are identical to the canonical scripted experiment, so
driving this runner with ScriptedDriver reproduces canonical receipts
byte-for-byte (asserted in tests).
"""

import json
import os
from datetime import datetime, timezone

from .agent.harness import Harness, RunResult
from .authority import PolicyAuthority
from .broker import CapabilityBroker
from .corpus import build_demo_corpus
from .capabilities import policy_version
from .evaluator import evaluate_task
from .negotiation import negotiation_telemetry
from .llm_metrics import aggregate_runs, denial_recovery
from .metrics import suite_metrics, task_metrics
from .receipts import Ledger
from .tasks import LogicalClock, RunContext, experiment_policy, task_suite


def _repeated_requests(ledger: Ledger, task_id: str) -> int:
    seen: set[tuple] = set()
    repeated = 0
    for r in ledger.by_task(task_id):
        if r.kind != "grant_request":
            continue
        key = (r.detail.get("resource"), r.detail.get("operation"),
               r.detail.get("projection"))
        if key in seen:
            repeated += 1
        seen.add(key)
    return repeated


def run_llm_experiment(driver_factory, out_dir: str, *, label: str, model: str,
                       base_url: str, runs: int = 5, tasks=None,
                       max_steps: int = 40, condition: str = "control") -> dict:
    """condition: "control" (three-method facade, current manifest) or
    "treatment" (facade additionally exposes capability discovery; the
    driver factory is expected to build discovery-aware drivers)."""
    if condition not in ("control", "treatment"):
        raise ValueError(f"unknown condition {condition!r}")
    discovery = condition == "treatment"
    tasks = tasks if tasks is not None else task_suite()
    os.makedirs(out_dir, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    run_records = []
    runs_reused: list[int] = []
    runs_executed: list[int] = []
    for i in range(1, runs + 1):
        run_id = f"{label}-run-{i}"
        run_dir = os.path.join(out_dir, f"run-{i}")
        os.makedirs(run_dir, exist_ok=True)
        sealed = os.path.join(run_dir, "metrics.json")
        if os.path.exists(sealed):
            # sealed run from an earlier invocation: reuse, never rerun
            with open(sealed) as fh:
                run_records.append(json.load(fh))
            runs_reused.append(i)
            continue
        runs_executed.append(i)
        receipts_path = os.path.join(run_dir, "receipts.jsonl")
        if os.path.exists(receipts_path):
            os.unlink(receipts_path)  # partial run: receipts re-created

        clock = LogicalClock()
        corpus = build_demo_corpus()
        ledger = Ledger(clock=clock.now, sink_path=receipts_path)
        authority = PolicyAuthority(experiment_policy(), clock=clock.now)
        broker = CapabilityBroker(corpus, authority, ledger, clock=clock.now)
        harness = Harness(broker, ledger, discovery=discovery)

        per_task: dict[str, dict] = {}
        success: dict[str, bool] = {}
        for task in tasks:
            driver = driver_factory(task)
            result = harness.run_task(task.task_id, driver, prompt=task.prompt,
                                      max_steps=max_steps)
            ok = task.check(RunContext(result, corpus, ledger))
            success[task.task_id] = ok
            record = task_metrics(ledger, task.task_id, ok)
            tele = driver.telemetry()
            tele["latency_total"] = sum(tele["latencies"])
            record["telemetry"] = tele
            record["denial_recovery"] = denial_recovery(ledger, task.task_id)
            record["repeated_grant_requests"] = _repeated_requests(
                ledger, task.task_id)
            record["abandoned"] = result.truncated
            record["answer"] = result.answer
            # receipt-grounded evaluation (docs/EVALUATOR.md): derived from
            # the receipt rows alone; the legacy `success` is kept as-is
            rows = [r.to_dict() for r in ledger.all()]
            record["receipt_grounded"] = evaluate_task(
                rows, task.task_id, task.prompt)
            record["negotiation"] = negotiation_telemetry(
                rows, task.task_id, authority.rules)
            per_task[task.task_id] = record

        suite = suite_metrics(per_task, success)
        suite["receipt_grounded"] = {
            "succeeded": sum(1 for r in per_task.values()
                             if r["receipt_grounded"]["outcome"] == "SUCCESS"),
            "unknown": sum(1 for r in per_task.values()
                           if r["receipt_grounded"]["outcome"] == "UNKNOWN"),
            "inadmissible": sum(
                1 for r in per_task.values()
                if r["receipt_grounded"]["admissibility"]["verdict"]
                == "INADMISSIBLE"),
        }
        run_record = {"run_id": run_id, "model": model, "label": label,
                      "per_task": per_task, "suite": suite}
        with open(os.path.join(run_dir, "metrics.json"), "w") as fh:
            json.dump(run_record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        run_records.append(run_record)

    aggregate = aggregate_runs(run_records)
    finished = datetime.now(timezone.utc).isoformat()
    meta = {
        "label": label, "model": model, "base_url": base_url, "runs": runs,
        "driver": "llm-adapter", "exploratory": True,
        "max_steps": max_steps,
        "condition": condition,
        "capability_discovery": discovery,
        "policy_version": policy_version(experiment_policy()),
        "runs_completed": runs_reused + runs_executed,
        "runs_reused": runs_reused,
        "runs_executed": runs_executed,
        "started_utc": started, "finished_utc": finished,
        "blackwall_revision": _git_revision(),
        "note": "exploratory real-model telemetry; not part of the normative "
                "evaluation; no credentials recorded",
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(out_dir, "aggregate.json"), "w") as fh:
        json.dump(aggregate, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(out_dir, "report.md"), "w") as fh:
        fh.write(_render_report(meta, aggregate, run_records))
    return {"meta": meta, "runs": run_records, "aggregate": aggregate}


def _git_revision() -> str:
    import subprocess
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=5).stdout.strip()
    except Exception:
        return "unknown"


def _fmt(stats: dict) -> str:
    return (f"{stats['mean']:.2f} (sd {stats['pstdev']:.2f}, "
            f"{stats['min']:.0f}..{stats['max']:.0f})")


def _render_report(meta: dict, aggregate: dict, run_records: list[dict]) -> str:
    s = aggregate["suite"]
    lines = [
        "# BLACKWALL real-model exploratory run",
        "",
        f"- label: {meta['label']}",
        f"- model: {meta['model']}",
        f"- endpoint: {meta['base_url']}",
        f"- runs: {meta['runs']} ({meta['started_utc']} .. {meta['finished_utc']})",
        f"- blackwall revision: {meta['blackwall_revision']}",
        "",
        "## Suite",
        "",
        f"- task success rate per run: "
        f"{['%.3f' % r for r in s['success_rate']['per_run']]}",
        f"- mean {s['success_rate']['mean']:.3f} "
        f"(sd {s['success_rate']['pstdev']:.3f})",
        "",
        "## Per task",
        "",
        "| task | success freq | grants req | broker calls | turns | latency s |"
        " unmediated | divergence |",
        "|---|---|---|---|---|---|---|"]
    for tid, t in sorted(aggregate["per_task"].items()):
        div = ", ".join(f"{k}:{v}" for k, v in t["divergence_totals"].items()
                        if v) or "none"
        lines.append(
            f"| {tid} | {t['succeeded']}/{t['runs']} | "
            f"{_fmt(t['grants_requested'])} | {_fmt(t['broker_calls'])} | "
            f"{_fmt(t['turns'])} | {_fmt(t['latency_total'])} | "
            f"{t['unmediated_attempts_total']} | {div} |")
    lines += ["", "## Denial recovery (totals over runs)", ""]
    for tid, t in sorted(aggregate["per_task"].items()):
        rec = {k: v for k, v in t["denial_recovery_totals"].items() if v}
        if rec:
            lines.append(f"- {tid}: {rec}")
    lines.append("")
    return "\n".join(lines)
