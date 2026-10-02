#!/usr/bin/env python3
"""Compare phase 4 conditions from receipts.

Usage: python3 run_phase4_report.py <control_dir> <treatment_dir>
           [--baseline results/exploratory/gpt-oss-20b-20261001T185916Z]
           [--out results/reevaluation/phase4-<name>]

Evaluates every sealed run of each condition with the receipt-grounded
evaluator, never pools conditions, and writes comparison.json + report.md.
Stored runs are never modified.
"""

import argparse
import json
import os
import sys

from beigewall.phase4 import compare, render


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("control_dir")
    p.add_argument("treatment_dir")
    p.add_argument("--baseline", default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    out = a.out or os.path.join("results", "reevaluation",
                                "phase4-" + os.path.basename(
                                    a.treatment_dir.rstrip("/")))
    canonical = os.path.realpath(os.path.join("results", "canonical"))
    if os.path.realpath(out).startswith(canonical):
        print("error: refusing to write into results/canonical", file=sys.stderr)
        return 2
    result = compare(a.control_dir, a.treatment_dir, a.baseline)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "comparison.json"), "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True, default=dict)
        fh.write("\n")
    text = render(result)
    with open(os.path.join(out, "report.md"), "w") as fh:
        fh.write(text)
    print(text.split("## Grant friction")[0])
    print(f"wrote {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
