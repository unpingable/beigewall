#!/usr/bin/env python3
"""Phase 5 matrix runner: four conditions, resumable, never overwriting.

Usage:
  BLACKWALL_LLM_BASE_URL=... BLACKWALL_LLM_MODEL=... \
  python3 run_phase5.py --root results/exploratory/phase5-<label> \
      [--runs 10] [--conditions control global denial task] [--attempts 8]
  python3 run_phase5.py --root ... --report [--anchor name=dir ...]

Each condition runs in <root>/<condition>/ through run_llm_experiment.py
(sealed runs are reused; a crashed run is re-executed). On an endpoint
failure the condition is re-invoked up to --attempts times. --report
evaluates every sealed run of every condition from receipts and writes
<root>/comparison/{comparison.json,report.md}. Missing cells are reported,
never substituted.
"""

import argparse
import json
import os
import subprocess
import sys
import time

CONDITIONS = ("control", "global", "denial", "task")


def sealed_runs(cond_dir: str) -> int:
    n = 0
    if not os.path.isdir(cond_dir):
        return 0
    for name in os.listdir(cond_dir):
        if name.startswith("run-") and os.path.exists(
                os.path.join(cond_dir, name, "metrics.json")):
            n += 1
    return n


def run_condition(root: str, condition: str, runs: int, attempts: int,
                  label: str) -> bool:
    out = os.path.join(root, condition)
    for attempt in range(1, attempts + 1):
        if sealed_runs(out) >= runs and os.path.exists(os.path.join(out, "aggregate.json")):
            print(f"[{condition}] sealed ({runs} runs)")
            return True
        print(f"[{condition}] attempt {attempt}: {sealed_runs(out)}/{runs} sealed")
        cmd = [sys.executable, "run_llm_experiment.py", "--runs", str(runs),
               "--label", f"{label}-{condition}", "--condition", condition,
               "--out", out]
        rc = subprocess.call(cmd)
        if rc == 0:
            return True
        time.sleep(30)
    print(f"[{condition}] INCOMPLETE after {attempts} attempts: "
          f"{sealed_runs(out)}/{runs} runs sealed", file=sys.stderr)
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--conditions", nargs="+", default=list(CONDITIONS))
    p.add_argument("--attempts", type=int, default=8)
    p.add_argument("--label", default="p5")
    p.add_argument("--report", action="store_true")
    p.add_argument("--anchor", action="append", default=[],
                   help="name=dir of a sealed earlier batch to show alongside")
    a = p.parse_args()
    canonical = os.path.realpath(os.path.join("results", "canonical"))
    if os.path.realpath(a.root).startswith(canonical):
        print("error: refusing to write into results/canonical", file=sys.stderr)
        return 2
    if a.report:
        from blackwall.phase5 import compare, render
        conds = {c: os.path.join(a.root, c) for c in a.conditions
                 if sealed_runs(os.path.join(a.root, c))}
        missing = [c for c in a.conditions if c not in conds]
        anchors = dict(x.split("=", 1) for x in a.anchor)
        result = compare(conds, anchors)
        result["missing_conditions"] = missing
        result["sealed_runs"] = {c: sealed_runs(os.path.join(a.root, c))
                                 for c in a.conditions}
        out = os.path.join(a.root, "comparison")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "comparison.json"), "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True, default=dict)
            fh.write("\n")
        text = render(result)
        if missing:
            text += f"\nMissing conditions (no sealed runs): {missing}\n"
        text += f"\nSealed runs per condition: {result['sealed_runs']}\n"
        with open(os.path.join(out, "report.md"), "w") as fh:
            fh.write(text)
        print(text.split("## Over-deference")[0])
        print(f"wrote {out}/")
        return 0
    if not os.environ.get("BLACKWALL_LLM_BASE_URL"):
        print("error: set BLACKWALL_LLM_BASE_URL/BLACKWALL_LLM_MODEL", file=sys.stderr)
        return 2
    ok = True
    for c in a.conditions:
        ok &= run_condition(a.root, c, a.runs, a.attempts, a.label)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
