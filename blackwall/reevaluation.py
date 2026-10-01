"""Re-evaluation of stored runs with the receipt-grounded evaluator.

Role: read saved `receipts.jsonl` / `metrics.json` pairs (the canonical run or
the `run-N/` directories of an exploratory batch), evaluate every task with
`blackwall.evaluator.evaluate_task`, and compare against the stored legacy
strict result (`per_task[task_id]["success"]`). Stored files are never
modified and the legacy result is never recomputed. Each (run, task) gap is
attributed to a cause per the "Gap attribution" table in docs/EVALUATOR.md.
Output is `evaluation.json` and `report.md` in a separate directory.
"""

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from blackwall.tasks import task_suite

REPO_ROOT = Path(__file__).resolve().parent.parent

CAUSES = ("agreement_success", "agreement_failure", "string_artifact",
          "unsupported_claim", "genuine_failure", "capability_failure",
          "evaluation_uncertainty")
GAP_CAUSES = ("string_artifact", "unsupported_claim", "genuine_failure",
              "capability_failure", "evaluation_uncertainty")


def discover_runs(results_dir) -> list[dict]:
    d = Path(results_dir)
    if (d / "receipts.jsonl").is_file() and (d / "metrics.json").is_file():
        name = d.resolve().name
        return [{"name": name if name != "." else "canonical",
                 "receipts_path": str(d / "receipts.jsonl"),
                 "metrics_path": str(d / "metrics.json")}]
    runs = []
    for sub in d.glob("run-*"):
        m = re.fullmatch(r"run-(\d+)", sub.name)
        if (m and (sub / "receipts.jsonl").is_file()
                and (sub / "metrics.json").is_file()):
            runs.append((int(m.group(1)), {
                "name": sub.name,
                "receipts_path": str(sub / "receipts.jsonl"),
                "metrics_path": str(sub / "metrics.json")}))
    return [r for _, r in sorted(runs, key=lambda x: x[0])]


def attribute_gap(legacy_strict: bool, evaluation: dict) -> str:
    """Attribute the old-strict vs receipt-grounded relationship to a cause."""
    outcome = evaluation["outcome"]
    if outcome == "UNKNOWN":
        return "evaluation_uncertainty"
    if outcome == "SUCCESS":
        return "agreement_success" if legacy_strict else "string_artifact"
    # outcome FAIL from here on
    if (evaluation["admissibility"]["verdict"] == "INADMISSIBLE"
            or evaluation["admissibility"].get("unsupported")):
        return "unsupported_claim"
    if evaluation["capability"]["verdict"] == "FAIL":
        return "capability_failure"
    # Legacy fail and not useful: both fail for the same reason. Legacy pass
    # but not useful: the new evaluator found a usefulness gap the old string
    # check missed. "agreement_failure" would be wrong there, so both cases
    # are reported as genuine_failure.
    return "genuine_failure"


def _load_rows(path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def reevaluate_run(run: dict) -> dict:
    from blackwall.evaluator import evaluate_task  # lazy: core may be absent

    rows = _load_rows(run["receipts_path"])
    with open(run["metrics_path"]) as f:
        metrics = json.load(f)
    per_task = {}
    for task in task_suite():
        legacy = bool(metrics["per_task"][task.task_id]["success"])
        ev = evaluate_task(rows, task.task_id, task.prompt)
        per_task[task.task_id] = {"legacy_strict": legacy, "evaluation": ev,
                                  "attribution": attribute_gap(legacy, ev)}
    return {"name": run["name"], "per_task": per_task}


def _no_factual_claims(e: dict) -> bool:
    if not (e.get("answer") or "").strip():
        return False
    cov = e.get("coverage")
    if cov is not None:
        return bool(cov.get("no_factual_claims"))
    return not any(c.get("kind") in ("fact", "foreign", "provenance",
                                     "effect_status")
                   for c in e.get("claims", []))


def _rate(n: int, total: int) -> float:
    return round(n / total, 4) if total else 0.0


def _counts(entries: list[tuple[bool, dict, str]]) -> dict:
    ev = [e for _, e, _ in entries]
    return {
        "old_strict": sum(1 for l, _, _ in entries if l),
        "receipt_grounded": sum(1 for e in ev if e["outcome"] == "SUCCESS"),
        "unknown": sum(1 for e in ev if e["outcome"] == "UNKNOWN"),
        "inadmissible": sum(1 for e in ev
                            if e["admissibility"]["verdict"] == "INADMISSIBLE"),
        "capability_fail": sum(1 for e in ev
                               if e["capability"]["verdict"] == "FAIL"),
        "not_useful": sum(1 for e in ev
                          if e["usefulness"]["verdict"] == "NOT_USEFUL"),
    }


def comparison_metrics(run_results: list[dict]) -> dict:
    entries, by_task = [], {}
    for run in run_results:
        for tid, t in run["per_task"].items():
            entry = (t["legacy_strict"], t["evaluation"], t["attribution"])
            entries.append(entry)
            by_task.setdefault(tid, []).append(entry)
    total = len(entries)
    c = _counts(entries)
    # coverage: answers with no factual claim at all (info-only or none),
    # and the total of sentences no claim touched — "not evaluated", never
    # "verified" (docs/EVALUATOR.md "Limitations")
    no_claims = sum(1 for _, e, _ in entries
                    if _no_factual_claims(e))
    unverified = sum(e.get("coverage", {}).get("unverified_sentences", 0)
                     for _, e, _ in entries)
    sentences = sum(e.get("coverage", {}).get("sentences", 0)
                    for _, e, _ in entries)
    degraded = sum(1 for _, e, _ in entries
                   if e["outcome"] == "SUCCESS" and e["usefulness"].get("degraded"))
    attribution = Counter(a for _, _, a in entries)
    per_task = {}
    for tid, es in by_task.items():
        row = _counts(es)
        row["runs"] = len(es)
        row["attribution"] = dict(sorted(Counter(a for _, _, a in es).items()))
        per_task[tid] = row
    counts = {
        "old_strict_success": c["old_strict"],
        "receipt_grounded_success": c["receipt_grounded"],
        "unknown_outcomes": c["unknown"],
        "unsupported_claim_failures": c["inadmissible"],
        "capability_failures": c["capability_fail"],
        "usefulness_failures": c["not_useful"],
        "no_claims_extracted": no_claims,
        "degraded_successes": degraded,
    }
    coverage = {"unverified_sentences": unverified, "sentences": sentences}
    return {
        "executions": total,
        "counts": counts,
        "rates": {k: _rate(v, total) for k, v in counts.items()},
        "coverage": coverage,
        "attribution": dict(sorted(attribution.items())),
        "per_task": dict(sorted(per_task.items())),
        "gap_decomposition": {k: attribution.get(k, 0) for k in GAP_CAUSES},
    }


def _git_revision() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def reevaluate_results_dir(results_dir, out_dir) -> dict:
    src = Path(results_dir).resolve()
    out = Path(out_dir).resolve()
    canonical = (REPO_ROOT / "results" / "canonical").resolve()
    if _inside(out, canonical) or _inside(out, src):
        raise ValueError(f"refusing to write inside stored results: {out}")
    runs = discover_runs(src)
    if not runs:
        raise ValueError(f"no runs found in {src}")
    run_results = [reevaluate_run(r) for r in runs]
    result = {
        "source": {"results_dir": str(src),
                   "runs": [r["name"] for r in runs],
                   "git_revision": _git_revision()},
        "runs": run_results,
        "comparison": comparison_metrics(run_results),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n")
    (out / "report.md").write_text(render_report(result))
    return result


def _trunc(text, n=80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n]


def _claim(c: dict) -> str:
    return f'{c.get("kind")}:{c.get("fact_id")} "{_trunc(c.get("text", ""))}"'


def headline_table(comparison: dict) -> str:
    n = comparison["executions"]
    c, r = comparison["counts"], comparison["rates"]
    rows = [("old strict success", "old_strict_success"),
            ("receipt-grounded success", "receipt_grounded_success"),
            ("unknown outcomes", "unknown_outcomes")]
    lines = [f"| measure | n / {n} | rate |", "|---|---|---|"]
    lines += [f"| {label} | {c[k]} | {r[k]:.3f} |" for label, k in rows]
    return "\n".join(lines)


def _dominant(hist: dict) -> str:
    if not hist:
        return "-"
    return sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def render_report(result: dict) -> str:
    cmp_ = result["comparison"]
    src = result.get("source", {})
    out = ["# Receipt-grounded re-evaluation", ""]
    out.append(f"Source: `{src.get('results_dir')}`; runs: "
               f"{', '.join(src.get('runs', []))}; revision: "
               f"`{src.get('git_revision')}`.")
    out += ["", "## Headline", "", headline_table(cmp_), ""]
    c = cmp_["counts"]
    out.append(f"Unsupported-claim failures: {c['unsupported_claim_failures']}; "
               f"capability failures: {c['capability_failures']}; "
               f"usefulness failures: {c['usefulness_failures']}; "
               f"no factual claims extracted: {c['no_claims_extracted']}; "
               f"unverified sentences: "
               f"{cmp_['coverage']['unverified_sentences']}/"
               f"{cmp_['coverage']['sentences']}; "
               f"degraded successes: {c['degraded_successes']}.")
    out += ["", "## Gap decomposition", "", "| cause | n |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in cmp_["gap_decomposition"].items()]
    out += ["", "## Per task", "",
            "| task | runs | old strict | receipt-grounded | inadmissible "
            "| capability-fail | not-useful | unknown | dominant attribution |",
            "|---|---|---|---|---|---|---|---|---|"]
    for tid, t in cmp_["per_task"].items():
        out.append(f"| {tid} | {t['runs']} | {t['old_strict']} | "
                   f"{t['receipt_grounded']} | {t['inadmissible']} | "
                   f"{t['capability_fail']} | {t['not_useful']} | "
                   f"{t['unknown']} | {_dominant(t['attribution'])} |")
    out += ["", "## Per run", ""]
    for run in result["runs"]:
        out += [f"### {run['name']}", ""]
        for tid, t in run["per_task"].items():
            e = t["evaluation"]
            out.append(f"- **{tid}**: outcome {e['outcome']}; capability "
                       f"{e['capability']['verdict']}; admissibility "
                       f"{e['admissibility']['verdict']}; usefulness "
                       f"{e['usefulness']['verdict']}; legacy_strict "
                       f"{t['legacy_strict']}; attribution {t['attribution']}")
            adm = e["admissibility"]
            if adm["verdict"] == "INADMISSIBLE":
                for cl in adm.get("unsupported", []):
                    out.append(f"  - unsupported {_claim(cl)}")
                out.append(f"  - answer: \"{_trunc(e.get('answer') or '', 120)}\"")
            if e["outcome"] == "UNKNOWN":
                for cl in adm.get("unknown", []):
                    out.append(f"  - unknown {_claim(cl)}")
            u = e["usefulness"]
            if u["verdict"] == "NOT_USEFUL":
                why = []
                for key in ("missing_required", "unobtained_required",
                            "effects_missing", "effects_partial"):
                    if u.get(key):
                        why.append(f"{key}={','.join(u[key])}")
                if (u.get("blocked_required") or u.get("effects_blocked")) \
                        and not u.get("reports_unavailability"):
                    why.append("blocked item not reported as unavailable")
                if not (e.get("answer") or "").strip():
                    why.append("empty answer")
                out.append(f"  - not useful: {'; '.join(why) or 'see usefulness'}")
            cap = e["capability"]
            if cap["verdict"] == "FAIL":
                out.append(f"  - capability: unmediated={cap['unmediated_attempts']} "
                           f"forbidden_disclosed={cap['forbidden_disclosed']} "
                           f"forbidden_effects={cap['forbidden_effects_landed']}")
        out.append("")
    return "\n".join(out) + "\n"
