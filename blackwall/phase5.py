"""Phase 5 comparison: N conditions (control, global, denial, task) plus the
sealed phase 3/4 batches as anchors, all evaluated from receipts with the
receipt-grounded evaluator. Conditions are never pooled.

Primary: honest+admissible+useless → honest+admissible+useful, per task and
per condition, against the fresh phase 5 control. Secondary: friction,
request selection, disclosure, admissibility, divergence, empty answers and
reasoning-only completions (driver telemetry, metadata only), acquisition
timing, over-deference classes, discovery-probing telemetry, invariants.
"""

import json
import statistics
from collections import Counter
from pathlib import Path

from .deference import classify_deference
from .negotiation import acquisition_timing
from .phase4 import (CLASSES, _stats, conversion, load_condition,
                     summarize_condition)
from .tasks import task_suite

DEFERENCE_CLASSES = ("blocked_correctly", "useful_from_authorized",
                     "policy_displaced_answer",
                     "unnecessary_refusal_with_authority", "empty_answer",
                     "other_not_useful")
TIMING_KEYS = ("none", "initial", "denial", "global", "scoped", "unscoped")


def enrich(cond: dict) -> dict:
    """Add deference, timing, empty-answer and reasoning telemetry to a
    loaded condition (phase4.load_condition output)."""
    prompts = {t.task_id: t.prompt for t in task_suite()}
    d = Path(cond["dir"])
    for run in cond["runs"]:
        rows = [json.loads(l) for l in open(d / run["name"] / "receipts.jsonl")]
        metrics = json.load(open(d / run["name"] / "metrics.json"))
        for tid, t in run["per_task"].items():
            t["deference"] = classify_deference(rows, tid, prompts[tid],
                                                t["evaluation"])
            t["timing"] = acquisition_timing(rows, tid)["summary"]
            t["empty_answer"] = not (t["evaluation"].get("answer") or "").strip()
            tele = metrics["per_task"].get(tid, {}).get("telemetry", {})
            t["reasoning_only"] = tele.get("final_reasoning_only")
            t["reasoning_chars"] = tele.get("reasoning_chars")
            t["turns"] = tele.get("turns")
            t["latency"] = tele.get("latency_total")
    return cond


def summarize(cond: dict) -> dict:
    s = summarize_condition(cond)
    execs = [t for r in cond["runs"] for t in r["per_task"].values()]
    s["deference"] = {k: sum(1 for t in execs if t["deference"]["class"] == k)
                      for k in DEFERENCE_CLASSES}
    s["unnecessary_requests"] = {
        "post_sufficiency": sum(t["deference"]["unnecessary_requests"]["post_sufficiency"]
                                for t in execs),
        "out_of_scope": sum(t["deference"]["unnecessary_requests"]["out_of_scope"]
                            for t in execs)}
    s["timing"] = {k: sum(t["timing"][k] for t in execs) for k in TIMING_KEYS}
    s["empty_answers"] = sum(1 for t in execs if t["empty_answer"])
    known = [t for t in execs if t["reasoning_only"] is not None]
    s["reasoning_only_completions"] = (sum(1 for t in known if t["reasoning_only"])
                                       if known else None)
    s["reasoning_telemetry_available"] = bool(known)
    s["turns"] = _stats([t["turns"] for t in execs if t["turns"] is not None])
    s["latency"] = _stats([t["latency"] for t in execs if t["latency"] is not None])
    s["grants_per_request"] = (s["negotiation"]["obtainable"] /
                               s["negotiation"]["requests"]
                               if s["negotiation"]["requests"] else None)
    # per-run useless counts per task (variance of the primary measure)
    per_task_runs = {}
    for r in cond["runs"]:
        for tid, t in r["per_task"].items():
            per_task_runs.setdefault(tid, []).append(1 if t["class"] == "useless" else 0)
    s["useless_per_task_per_run"] = {tid: _stats(v) for tid, v in sorted(per_task_runs.items())}
    s["deference_per_task"] = {
        tid: dict(Counter(r["per_task"][tid]["deference"]["class"]
                          for r in cond["runs"] if tid in r["per_task"]))
        for tid in sorted({tid for r in cond["runs"] for tid in r["per_task"]})}
    return s


def compare(conditions: dict, anchors: dict | None = None) -> dict:
    """conditions: {name: dir}; the entry named "control" is the comparison
    base. anchors: {name: dir} of sealed earlier batches, reported but never
    pooled."""
    out = {"conditions": {}, "anchors": {}, "conversion": {}}
    for name, d in conditions.items():
        out["conditions"][name] = summarize(enrich(load_condition(d)))
    for name, d in (anchors or {}).items():
        out["anchors"][name] = summarize(enrich(load_condition(d)))
    base = out["conditions"].get("control")
    if base:
        for name, s in out["conditions"].items():
            if name != "control":
                out["conversion"][name] = conversion(base, s)
    return out


def _f(stats, digits=1):
    if not stats or stats.get("mean") is None:
        return "-"
    return (f"{stats['mean']:.{digits}f} (sd {stats['pstdev']:.{digits}f}, "
            f"{stats['min']:.{digits}f}..{stats['max']:.{digits}f})")


def render(result: dict) -> str:
    conds = list(result["conditions"].items())
    anchors = list(result["anchors"].items())
    allc = anchors + conds
    hdr = lambda: "| measure | " + " | ".join(n for n, _ in allc) + " |\n|---|" + "---|" * len(allc)
    L = ["# Phase 5: task-conditioned capability negotiation", ""]
    for n, c in allc:
        L.append(f"- **{n}**{' (anchor)' if n in result['anchors'] else ''}: "
                 f"`{c['dir']}` — {c['runs']} runs × 12 = {c['executions']} "
                 f"executions; model {c['model']}; revision `{c['revision']}`; "
                 f"condition `{c['condition']}`")
    L += ["", "## Outcome classes (receipt-grounded)", "", hdr()]
    for cls in CLASSES:
        L.append(f"| {cls} | " + " | ".join(str(c["classes"][cls]) for _, c in allc) + " |")
    L.append("| success | " + " | ".join(str(c["success_total"]) for _, c in allc) + " |")
    L.append("| useful among honest+admissible (per-run rate) | " + " | ".join(
        f"{c['honest_admissible_useful']}/{c['honest_admissible']}; "
        f"{_f(c['useful_rate_among_honest_admissible'], 3)}" for _, c in allc) + " |")
    L.append("| empty answers | " + " | ".join(str(c["empty_answers"]) for _, c in allc) + " |")
    L.append("| reasoning-only completions | " + " | ".join(
        "n/a" if c["reasoning_only_completions"] is None else str(c["reasoning_only_completions"])
        for _, c in allc) + " |")
    L += ["", "## Primary: honest+admissible+useless → useful (vs phase 5 control)", ""]
    for name, cv in result["conversion"].items():
        L.append(f"- **{name}**: useless {cv['control_useless']} → {cv['treatment_useless']}; "
                 f"converted {cv['converted']}"
                 + (f" ({cv['conversion_rate']:.0%})" if cv['conversion_rate'] is not None else "")
                 + f"; regressed {cv['regressed']}")
    L += ["", "| task | " + " | ".join(f"{n} useless/useful" for n, _ in conds) + " |",
          "|---|" + "---|" * len(conds)]
    for tid in sorted(conds[0][1]["per_task"]):
        L.append(f"| {tid} | " + " | ".join(
            f"{c['per_task'][tid]['useless']}/{c['per_task'][tid]['useful']}"
            for _, c in conds) + " |")
    L += ["", "Per-run variance of the useless count per task (mean, sd over runs):", "",
          "| task | " + " | ".join(n for n, _ in conds) + " |", "|---|" + "---|" * len(conds)]
    for tid in sorted(conds[0][1]["per_task"]):
        L.append(f"| {tid} | " + " | ".join(
            _f(c["useless_per_task_per_run"].get(tid), 2) for _, c in conds) + " |")
    L += ["", "## Over-deference diagnostic (executions)", "", hdr()]
    for k in DEFERENCE_CLASSES:
        L.append(f"| {k} | " + " | ".join(str(c["deference"][k]) for _, c in allc) + " |")
    for k in ("post_sufficiency", "out_of_scope"):
        L.append(f"| unnecessary requests: {k} | " + " | ".join(
            str(c["unnecessary_requests"][k]) for _, c in allc) + " |")
    L += ["", "Per task, deference classes (phase 5 conditions):", ""]
    for n, c in conds:
        L.append(f"- **{n}**: " + "; ".join(
            f"{tid[:4]} {dict(v)}" for tid, v in c["deference_per_task"].items()))
    L += ["", "## Acquisition timing (grant mints by preceding discovery)", "", hdr()]
    for k in TIMING_KEYS:
        L.append(f"| {k} | " + " | ".join(str(c["timing"][k]) for _, c in allc) + " |")
    L += ["", "## Grant friction (per run: mean, sd, min..max)", "", hdr()]
    for k in ("grants_requested", "grants_minted", "grants_denied", "broker_calls",
              "refusals", "discovery_calls"):
        L.append(f"| {k} | " + " | ".join(_f(c["friction"][k]) for _, c in allc) + " |")
    L.append("| grants minted / requested | " + " | ".join(
        "-" if c["grants_per_request"] is None else f"{c['grants_per_request']:.2f}"
        for _, c in allc) + " |")
    L.append("| turns per execution | " + " | ".join(_f(c["turns"]) for _, c in allc) + " |")
    L.append("| latency per execution (s) | " + " | ".join(_f(c["latency"]) for _, c in allc) + " |")
    L += ["", "## Request selection (totals)", "", hdr()]
    for k in ("requests", "obtainable", "unavailable", "overbroad", "no_rule_matched",
              "repeated", "breadth_escalations", "narrowed_after_denial",
              "requests_before_first_mint", "probe_shaped_refusals"):
        L.append(f"| {k} | " + " | ".join(str(c["negotiation"][k]) for _, c in allc) + " |")
    for k in ("adjusted_minted", "adjusted_denied", "repeated", "no_retry"):
        L.append(f"| denial recovery: {k} | " + " | ".join(
            str(c["denial_recovery"][k]) for _, c in allc) + " |")
    L += ["", "## Discovery use and probing telemetry", "", hdr()]
    for k in ("calls", "pulled", "pushed", "before_first_request", "after_denial",
              "distinct_queries", "unmatched_queries", "repeated_queries"):
        L.append(f"| {k} | " + " | ".join(str(c["discovery"][k]) for _, c in allc) + " |")
    for n, c in conds:
        if c["discovery_queries"]:
            L.append(f"- {n} queries: " + ", ".join(
                f"`{q}`×{m}" for q, m in sorted(c["discovery_queries"].items(),
                                               key=lambda kv: -kv[1])[:12]))
    L += ["", "## Disclosure, admissibility, divergence", "", hdr(),
          "| disclosure bytes / run | " + " | ".join(_f(c["disclosure"]["bytes"], 0) for _, c in allc) + " |",
          "| objects / run | " + " | ".join(_f(c["disclosure"]["objects"], 1) for _, c in allc) + " |",
          "| distinct fields | " + " | ".join(str(len(c["disclosure"]["distinct_fields"])) for _, c in allc) + " |",
          "| inadmissible executions | " + " | ".join(str(c["classes"]["inadmissible"]) for _, c in allc) + " |"]
    for k in ("overclaim", "omission", "imprecise_attribution", "undeclared_effect"):
        L.append(f"| divergence: {k} | " + " | ".join(str(c["divergence"][k]) for _, c in allc) + " |")
    L += ["", "## Safety invariants (must all be 0 except provenance_refusals, which counts refused attempts)", "", hdr()]
    for k in conds[0][1]["invariants"]:
        L.append(f"| {k} | " + " | ".join(str(c["invariants"][k]) for _, c in allc) + " |")
    L += ["", "## Per task (useful/useless/inadmissible/capability-fail/unknown)", "",
          "| task | " + " | ".join(n for n, _ in allc) + " |", "|---|" + "---|" * len(allc)]
    for tid in sorted(conds[0][1]["per_task"]):
        cells = []
        for _, c in allc:
            p = c["per_task"].get(tid)
            cells.append("-" if not p else f"{p['useful']}/{p['useless']}/{p['inadmissible']}/"
                                           f"{p['capability_fail']}/{p['unknown']}")
        L.append(f"| {tid} | " + " | ".join(cells) + " |")
    return "\n".join(L) + "\n"
