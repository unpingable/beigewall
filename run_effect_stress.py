#!/usr/bin/env python3
"""Adversarial effect-stress harness (phase 6B, docs/RESOURCE_BOUNDS.md).

Drives the hostile client in `blackwall.ipc.stress` against a running
`blackwall.ipc.server` (the caller starts it; this never spawns a server)
and counts effects that landed from the trusted ledger, not from what the
client saw.

  python3 run_effect_stress.py --socket S --ledger L --task t8-send-reply \
      --attack matrix --n 50 --workers 20 \
      --out results/exploratory/phase6b-stress/<name>.json

`matrix` runs every attack in sequence against ONE server and ONE task, so
task-scoped budgets accumulate across attacks: each row's "landed" is the
delta of effect receipts during that attack only.

`--as-process --no-begin` is the second hostile process of the
untrusted-process-restart attack: it continues an already active task
(never calls begin_task), knows no ledger, and prints its outcomes as one
JSON line on stdout.
"""

import argparse
import json
import os
import sys
import time

from blackwall.ipc import stress as S

ATTACKS = ("matrix", "sequential", "same_grant", "concurrent", "reconnect",
           "second_process", "replay", "forged_counter", "forged_task",
           "forged_grant", "reordered")
MATRIX_ORDER = ("same_grant", "sequential", "reconnect", "second_process",
                "concurrent", "replay", "forged_counter", "forged_task",
                "forged_grant", "reordered")
EXPECTED = {
    "same_grant": "1 effect, then 'effect grant consumed'",
    "sequential": "<= cap, then 'budget exhausted'",
    "reconnect": "counters unchanged",
    "second_process": "counters unchanged",
    "concurrent": "<= cap, no double consumption",
    "replay": "'effect grant consumed'",
    "forged_counter": "protocol unknown_arg",
    "forged_task": "task_not_active / task_closed",
    "forged_grant": "'unknown grant'",
    "reordered": "'unknown grant' before the grant",
}


def run_attack(sess, name: str, n: int, workers: int) -> dict:
    if name == "sequential":
        return S.sequential_fresh_grants(sess, n)
    if name == "same_grant":
        return S.repeat_same_grant(sess, n)
    if name == "concurrent":
        return S.concurrent(sess, n, workers)
    if name == "reconnect":
        return S.reconnect_between(sess, n)
    if name == "second_process":
        return S.second_process(sess, n)
    if name == "replay":
        return S.replay_effect_frame(sess, n)
    if name == "forged_counter":
        return S.forged_counter(sess)
    if name == "forged_task":
        return S.forged_task_id(sess)
    if name == "forged_grant":
        return S.forged_grant_id(sess)
    if name == "reordered":
        return S.reordered(sess)
    raise ValueError(name)


def observed(sess, name, n, workers, ledger, task) -> dict:
    before = S.count_landed(ledger, task) if ledger else None
    brefs = len(S.budget_refusals(ledger, task)) if ledger else None
    result = run_attack(sess, name, n, workers)
    result.update(S.summarize(result))
    if ledger:
        result["landed_delta"] = S.count_landed(ledger, task) - before
        result["landed_total_after"] = before + result["landed_delta"]
        result["budget_refusals_delta"] = len(S.budget_refusals(ledger, task)) - brefs
    result["expected"] = EXPECTED.get(name)
    return result


def _fmt(counts: dict) -> str:
    if not counts:
        return "-"
    return "; ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def table(rows: list[dict]) -> str:
    out = ["| attack | attempts | client-observed successes | effects landed (trusted "
           "receipts, delta) | refusal reasons | protocol rejections |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        landed = r.get("landed_delta")
        out.append(f"| {r['attack']} | {r['attempts']} | {r['client_successes']} | "
                   f"{'?' if landed is None else landed} | {_fmt(r['refusals'])} | "
                   f"{_fmt(r['protocol_rejections'])} |")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--socket", required=True)
    p.add_argument("--ledger", default=None,
                   help="trusted ledger, read by the observer to count landed effects "
                        "(required for matrix; never given to --as-process)")
    p.add_argument("--task", default=S.DEFAULT_TASK)
    p.add_argument("--attack", choices=ATTACKS, default="matrix")
    p.add_argument("--n", type=int, default=50)
    p.add_argument("--workers", type=int, default=20)
    p.add_argument("--out", default=None)
    p.add_argument("--as-process", action="store_true",
                   help="print outcomes as one JSON line (second hostile process)")
    p.add_argument("--no-begin", action="store_true",
                   help="continue an already active task: never call begin_task")
    p.add_argument("--prompt-hash", default=None,
                   help="provenance an attacker persisted across its own restart")
    a = p.parse_args(argv)
    if a.attack == "matrix" and not a.ledger:
        p.error("--attack matrix needs --ledger (landed effects are counted from receipts)")
    if a.as_process and a.ledger:
        p.error("--as-process never receives the ledger path")

    sess = S.StressSession(a.socket, a.task, begin=not a.no_begin, prompt_hash=a.prompt_hash)
    started = time.time()
    try:
        names = MATRIX_ORDER if a.attack == "matrix" else (a.attack,)
        rows = [observed(sess, name, a.n, a.workers, a.ledger, a.task) for name in names]
    finally:
        sess.close()

    summary = {
        "harness": "run_effect_stress.py", "task": a.task, "attack": a.attack,
        "n": a.n, "workers": a.workers, "started_at": started,
        "note": ("matrix: all attacks run in sequence against one server and one task; "
                 "task-scoped budgets accumulate across attacks, so later rows see the "
                 "budget already consumed by earlier ones. 'landed_delta' counts effect "
                 "receipts in the trusted ledger during each attack.")
                if a.attack == "matrix" else None,
        "attacks": rows,
    }
    if a.ledger:
        summary["landed_total"] = S.count_landed(a.ledger, a.task)
        summary["budget_refusals_total"] = len(S.budget_refusals(a.ledger, a.task))
        summary["resource_policy"] = [{k: v for k, v in r.items()
                                       if k in ("effect_count", "task_effects", "rate")}
                                      for r in S.resource_policy_rows(a.ledger)]
        effects = S.effect_rows(a.ledger, a.task)
        grant_ids = [r.get("grant_id") for r in effects]
        summary["double_consumed_grants"] = len(grant_ids) - len(set(grant_ids))

    if a.as_process:
        print(json.dumps({"outcomes": rows[0]["outcomes"], "summary": {
            k: rows[0][k] for k in ("attempts", "client_successes", "refusals",
                                    "protocol_rejections")}}))
    else:
        print(table(rows))
        if a.attack == "matrix":
            print("\nnote: budgets are cumulative across rows (one server, one task).")
        if a.ledger:
            print(f"landed total (receipts): {summary['landed_total']}; "
                  f"budget refusals: {summary['budget_refusals_total']}; "
                  f"grants consumed twice: {summary['double_consumed_grants']}; "
                  f"resource_policy: {summary['resource_policy'] or 'none (unbounded)'}")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
