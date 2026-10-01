#!/usr/bin/env python3
"""Re-evaluate stored BLACKWALL runs with the receipt-grounded evaluator.

Usage: python3 run_reevaluation.py <results_dir> [--out results/reevaluation/<basename>]

<results_dir> is either a single run directory (receipts.jsonl + metrics.json,
e.g. results/canonical) or a batch containing run-N/ directories. Stored
results are read-only inputs; evaluation.json and report.md are written to
--out, which may not lie inside results/canonical or the source directory.
"""

import argparse
from pathlib import Path

from blackwall.reevaluation import headline_table, reevaluate_results_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    base = Path(args.results_dir).resolve().name
    out = args.out or f"results/reevaluation/{base}"
    result = reevaluate_results_dir(args.results_dir, out)
    print(f"wrote {out}/")
    print(headline_table(result["comparison"]))


if __name__ == "__main__":
    main()
