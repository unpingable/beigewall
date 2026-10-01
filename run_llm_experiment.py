#!/usr/bin/env python3
"""Run the BLACKWALL task suite under a real LLM endpoint (exploratory).

Environment (required):
  BLACKWALL_LLM_BASE_URL   OpenAI-compatible base URL, e.g. http://localhost:11434/v1
  BLACKWALL_LLM_API_KEY    required for non-loopback endpoints; unused for loopback
  BLACKWALL_LLM_MODEL      model identifier (or pass --model)

Example:
  BLACKWALL_LLM_BASE_URL=http://localhost:11434/v1 \
  BLACKWALL_LLM_MODEL=qwen3:8b \
  python3 run_llm_experiment.py --runs 5

Canonical results are never written by this command. Output goes to a clearly
named exploratory directory (default: results/exploratory/<label>-<utc>/).
"""

import argparse
import os
import sys
from datetime import datetime, timezone

from blackwall.agent.drivers import LLMDriver
from blackwall.llm_experiment import run_llm_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--model", default=os.environ.get("BLACKWALL_LLM_MODEL"))
    parser.add_argument("--label", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--condition", choices=("control", "treatment"),
                        default="control",
                        help="treatment exposes capability discovery "
                             "(phase 4); control is the phase 2/3 setup")
    args = parser.parse_args()

    probe = LLMDriver.from_env()
    if probe is None:
        print("error: set BLACKWALL_LLM_BASE_URL (and BLACKWALL_LLM_API_KEY "
              "for non-loopback endpoints); refusing to run unconfigured",
              file=sys.stderr)
        return 2
    model = args.model or probe._model
    label = args.label or model.replace("/", "_").replace(":", "_")

    base_url = os.environ["BLACKWALL_LLM_BASE_URL"]
    api_key = os.environ.get("BLACKWALL_LLM_API_KEY", "")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or os.path.join("results", "exploratory",
                                   f"{label}-{stamp}")
    canonical = os.path.realpath(os.path.join("results", "canonical"))
    if os.path.realpath(out).startswith(canonical + os.sep) or \
            os.path.realpath(out) == canonical:
        print("error: refusing to write into results/canonical", file=sys.stderr)
        return 2

    def factory(_task):
        return LLMDriver(base_url, api_key, model,
                         discovery=args.condition == "treatment")

    print(f"running {args.runs} exploratory run(s) of 12 tasks "
          f"against {model} @ {base_url} [condition={args.condition}]")
    print(f"output: {out}")
    result = run_llm_experiment(factory, out, label=label, model=model,
                                base_url=base_url, runs=args.runs,
                                max_steps=args.max_steps,
                                condition=args.condition)
    agg = result["aggregate"]
    s = agg["suite"]["success_rate"]
    print(f"success rate per run: {['%.2f' % r for r in s['per_run']]} "
          f"(mean {s['mean']:.3f}, sd {s['pstdev']:.3f})")
    for tid, t in sorted(agg["per_task"].items()):
        print(f"  {tid}: {t['succeeded']}/{t['runs']} "
              f"grants={t['grants_requested']['mean']:.1f} "
              f"unmediated={t['unmediated_attempts_total']}")
    print(f"wrote {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
