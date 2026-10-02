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
    # effects performed in tasks whose contract calls for no effect: not
    # forbidden by policy, but world-changing actions the user never asked
    # for (a watch metric; see FINDINGS_PHASE5)
    from .contracts import answer_contracts
    no_effect = {tid for tid, c in answer_contracts().items() if not c.required_effects}
    s["unrequested_effects"] = sum(t["metrics"]["effects_performed"]
                                   for r in cond["runs"]
                                   for tid, t in r["per_task"].items() if tid in no_effect)
    s["unrequested_effects_by_task"] = dict(Counter(
        tid[:3] for r in cond["runs"] for tid, t in r["per_task"].items()
        if tid in no_effect for _ in range(t["metrics"]["effects_performed"])))
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


def _per_run(cond_summary_input: dict, key: str) -> list[float]:
    """Per-run values of a primary measure from an enriched condition."""
    out = []
    for r in cond_summary_input["runs"]:
        ts = list(r["per_task"].values())
        ha = [t for t in ts if t["class"] in ("useful", "useless")]
        if key == "useful_rate_ha":
            out.append(sum(1 for t in ha if t["class"] == "useful") / len(ha) if ha else 0.0)
        elif key == "useless":
            out.append(sum(1 for t in ts if t["class"] == "useless"))
        elif key == "useful":
            out.append(sum(1 for t in ts if t["class"] == "useful"))
        elif key == "empty":
            out.append(sum(1 for t in ts if t["empty_answer"]))
        elif key == "grants_requested":
            out.append(sum(t["metrics"]["grants_requested"] for t in ts))
    return out


def permutation_test(a: list[float], b: list[float]) -> dict:
    """Exact two-sided permutation test on the difference of means (all
    C(n+m, n) relabelings when that is ≤ 200k, else deterministic
    seeded sampling). Deterministic; stdlib only."""
    import itertools
    import math
    import random
    pooled = list(a) + list(b)
    n, total = len(a), len(pooled)
    observed = statistics.mean(a) - statistics.mean(b) if a and b else 0.0
    if not a or not b:
        return {"observed_diff": observed, "p_two_sided": None, "method": "n/a"}
    count = 0
    combos = math.comb(total, n)
    if combos <= 200_000:
        hits = 0
        for idx in itertools.combinations(range(total), n):
            sa = [pooled[i] for i in idx]
            rest = [pooled[i] for i in range(total) if i not in set(idx)]
            if abs(statistics.mean(sa) - statistics.mean(rest)) >= abs(observed) - 1e-12:
                hits += 1
            count += 1
        method = f"exact ({combos} relabelings)"
    else:
        rng = random.Random(20261002)
        hits = 0
        for _ in range(20_000):
            rng.shuffle(pooled)
            sa, rest = pooled[:n], pooled[n:]
            if abs(statistics.mean(sa) - statistics.mean(rest)) >= abs(observed) - 1e-12:
                hits += 1
            count += 1
        method = "sampled (20000, seed 20261002)"
    return {"observed_diff": observed, "p_two_sided": hits / count, "method": method,
            "n_a": n, "n_b": len(b)}


def per_run_tests(enriched: dict, base_name: str = "control") -> dict:
    base = enriched.get(base_name)
    if base is None:
        return {}
    out = {}
    for name, cond in enriched.items():
        if name == base_name:
            continue
        out[name] = {}
        for key in ("useful_rate_ha", "useless", "useful", "empty", "grants_requested"):
            out[name][key] = permutation_test(_per_run(cond, key), _per_run(base, key))
    return out


def compare(conditions: dict, anchors: dict | None = None) -> dict:
    """conditions: {name: dir}; the entry named "control" is the comparison
    base. anchors: {name: dir} of sealed earlier batches, reported but never
    pooled."""
    out = {"conditions": {}, "anchors": {}, "conversion": {}}
    enriched, enriched_anchors = {}, {}
    for name, d in conditions.items():
        enriched[name] = enrich(load_condition(d))
        out["conditions"][name] = summarize(enriched[name])
    for name, d in (anchors or {}).items():
        enriched_anchors[name] = enrich(load_condition(d))
        out["anchors"][name] = summarize(enriched_anchors[name])
    out["per_run_tests"] = per_run_tests(enriched)
    # batch drift: every anchor against the fresh control, same tests
    out["anchor_tests"] = per_run_tests({**enriched_anchors, "control": enriched.get("control")}) \
        if "control" in enriched else {}
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
    if result.get("per_run_tests"):
        L += ["", "Per-run permutation tests vs control (difference of per-run means; "
              "two-sided p):", "", "| condition | measure | diff | p | method |",
              "|---|---|---|---|---|"]
        for name, tests in result["per_run_tests"].items():
            for key, t in tests.items():
                L.append(f"| {name} | {key} | {t['observed_diff']:+.3f} | "
                         f"{'-' if t['p_two_sided'] is None else f'{t['p_two_sided']:.3f}'} | {t['method']} |")
        if result.get("anchor_tests"):
            L += ["", "Batch drift: sealed anchors vs the phase 5 control (same tests):", "",
                  "| anchor | measure | diff | p |", "|---|---|---|---|"]
            for name, tests in result["anchor_tests"].items():
                for key in ("useful_rate_ha", "useless", "empty"):
                    t = tests[key]
                    L.append(f"| {name} | {key} | {t['observed_diff']:+.3f} | "
                             f"{'-' if t['p_two_sided'] is None else f'{t['p_two_sided']:.3f}'} |")
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
    L.append("| unrequested effects (watch: effects in tasks that call for none) | "
             + " | ".join(f"{c['unrequested_effects']} {c['unrequested_effects_by_task'] or ''}".strip()
                          for _, c in allc) + " |")
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
