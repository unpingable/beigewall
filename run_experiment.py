#!/usr/bin/env python3
"""Run the BeigeWall experiment.

Usage: python3 run_experiment.py [--out results/canonical]

The run is deterministic: metrics.json and receipts.jsonl are byte-identical
across runs. Set BEIGEWALL_LLM_BASE_URL/BEIGEWALL_LLM_API_KEY only to exercise
the exploratory LLM driver; the canonical results always come from the
scripted driver.
"""

import argparse

from beigewall.experiment import run_experiment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="results/canonical")
    args = parser.parse_args()
    metrics = run_experiment(args.out)
    suite = metrics["suite"]
    print(f"wrote {args.out}/")
    print(f"task_success_rate: {suite['task_success_rate']:.3f} "
          f"({suite['tasks_succeeded']}/{suite['tasks_total']})")
    print(f"tasks failed: {', '.join(suite['tasks_failed']) or 'none'}")
    print(f"grants requested: {suite['grants_requested_total']}; "
          f"denied: {suite['grants_denied_total']}; "
          f"refusals: {suite['refused_operations_total']}; "
          f"unmediated attempts: {suite['unmediated_attempts_total']}")
    u = suite["usefulness_under_denial"]
    print(f"usefulness under denial: {u['succeeded_despite_denial']}/"
          f"{u['tasks_facing_denial']} tasks facing denial still succeeded")
    print(f"divergence totals: {suite['narrative_divergence_totals']}")


if __name__ == "__main__":
    main()
