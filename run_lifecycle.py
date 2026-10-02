#!/usr/bin/env python3
"""Run the Phase 5B lifecycle trajectory suite (exploratory) and report.

Usage:
  BLACKWALL_LLM_BASE_URL=... BLACKWALL_LLM_MODEL=... \
  python3 run_lifecycle.py --out results/exploratory/phase5b-<label> \
      [--trajectories 20] [--condition control|global|denial|task] \
      [--driver llm|scripted] [--attempts 8]
  python3 run_lifecycle.py --out results/exploratory/phase5b-<label> --report

Each trajectory writes <out>/<id>/receipts.jsonl and is sealed by an atomic
<out>/<id>/result.json. Sealed trajectories are reused; a crashed one is
re-run. On an endpoint RuntimeError the suite is retried up to --attempts
times with 30 s sleeps. Canonical results are never written.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
_sleep = time.sleep

PRIMARY = (
    ("stale", "stale-authority attempts"),
    ("replay", "replay attempts"),
    ("wrong_scope", "wrong-scope attempts"),
    ("forged", "forged/substituted attempts"),
    ("cross_task", "cross-task carry-over"),
    ("duplicate_acquisitions", "duplicate acquisitions"),
    ("duplicate_effects", "duplicate effects"),
)
_ALIASES = {
    "stale": ("stale", "stale_attempts", "stale_authority_attempts"),
    "replay": ("replay", "replay_attempts"),
    "wrong_scope": ("wrong_scope", "wrong_scope_attempts"),
    "forged": ("forged", "forged_attempts", "forged_substituted_attempts"),
    "cross_task": ("cross_task", "cross_task_attempts", "cross_task_carryover",
                   "cross_task_carry_over", "cross_task_uses"),
    "duplicate_acquisitions": ("duplicate_acquisitions",),
    "duplicate_effects": ("duplicate_effects",),
    "restarts": ("restarts", "session_restarts"),
    "transport_losses": ("transport_losses", "transport_loss"),
}


def _is_canonical(out: str) -> bool:
    canonical = os.path.realpath(os.path.join(HERE, "results", "canonical"))
    canonical_cwd = os.path.realpath(os.path.join("results", "canonical"))
    real = os.path.realpath(out)
    return any(real == c or real.startswith(c + os.sep)
               for c in (canonical, canonical_cwd))


def _git_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE,
                                       text=True).strip()
    except Exception:
        return "unknown"


def _write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, sort_keys=True, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _load_sealed(out: str, tid: str):
    try:
        with open(os.path.join(out, tid, "result.json")) as fh:
            rec = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return rec if isinstance(rec, dict) and "metrics" in rec else None


def _disagreements(disagreements, rows, tid):
    try:
        return list(disagreements(rows, tid))
    except TypeError:
        return list(disagreements(rows))


def _run_one(traj, out, driver_factory, discovery_mode, max_steps):
    from blackwall.lifecycle import run_trajectory
    from blackwall.lifecycle_metrics import disagreements, trajectory_metrics
    tdir = os.path.join(out, traj.trajectory_id)
    os.makedirs(tdir, exist_ok=True)
    sink = os.path.join(tdir, "receipts.jsonl")
    if os.path.exists(sink):
        os.remove(sink)  # crashed earlier attempt: re-run from scratch
    res = run_trajectory(traj, driver_factory, discovery_mode=discovery_mode,
                         sink_path=sink, max_steps=max_steps)
    rows = res["rows"]
    rec = {k: v for k, v in res.items() if k != "rows"}
    rec["metrics"] = trajectory_metrics(rows, traj.trajectory_id)
    rec["disagreements"] = _disagreements(disagreements, rows,
                                          traj.trajectory_id)
    _write_json_atomic(os.path.join(tdir, "result.json"), rec)


def run_suite(trajectories, out, driver_factory, discovery_mode, attempts,
              max_steps=40) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            for traj in trajectories:
                if _load_sealed(out, traj.trajectory_id) is not None:
                    print(f"[{traj.trajectory_id}] sealed")
                    continue
                print(f"[{traj.trajectory_id}] running")
                _run_one(traj, out, driver_factory, discovery_mode, max_steps)
            return True
        except RuntimeError as exc:
            print(f"attempt {attempt}/{attempts}: endpoint error: {exc}",
                  file=sys.stderr)
            if attempt < attempts:
                _sleep(30)
    return False


def _metric(m: dict, name: str):
    for k in _ALIASES.get(name, (name,)):
        v = m.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, (list, dict)):
            return len(v)
    return 0


def _recovery(m: dict) -> tuple[int, int]:
    r = m.get("recovery")
    if isinstance(r, dict):
        n = r.get("refusals_followed_by_fresh_mint_and_success",
                  r.get("succeeded", r.get("successes", r.get("n", 0))))
        d = r.get("refusals_total",
                  r.get("opportunities", r.get("total", r.get("m", 0))))
        return int(n or 0), int(d or 0)
    n = m.get("recovery_successes", m.get("recoveries", 0))
    d = m.get("recovery_opportunities", n)
    return int(n or 0), int(d or 0)


def _useful(rec: dict) -> tuple[int, int]:
    ev = rec.get("evaluations") or {}
    return (sum(1 for e in ev.values() if e.get("outcome") == "SUCCESS"),
            len(ev))


def _stats(vals) -> str:
    if not vals:
        return "n/a"
    return (f"total {sum(vals)}; mean {statistics.fmean(vals):.2f}; "
            f"sd {statistics.pstdev(vals):.2f}; min {min(vals)}; "
            f"max {max(vals)}")


def build_report(out: str, trajectories) -> tuple[str, dict]:
    from blackwall.lifecycle_suite import describe
    sealed, missing = {}, []
    for t in trajectories:
        rec = _load_sealed(out, t.trajectory_id)
        if rec is None:
            missing.append(t.trajectory_id)
        else:
            sealed[t.trajectory_id] = rec
    agg = None
    if sealed:
        try:
            from blackwall.lifecycle_metrics import aggregate
            agg = aggregate(list(sealed.values()))
        except ImportError:
            agg = None
    ids = sorted(sealed)
    mets = {i: sealed[i].get("metrics", {}) for i in ids}
    inv_names = sorted({k for m in mets.values()
                        for k in (m.get("invariants") or {})})
    inv_tot = {k: sum(_metric(mets[i].get("invariants") or {}, k)
                      for i in ids) for k in inv_names}
    L = ["# Phase 5B lifecycle report", "",
         f"Sealed trajectories: {len(ids)}/{len(trajectories)}"]
    if missing:
        L.append("")
        L.append("**Missing/unsealed:** " + ", ".join(missing))
    L += ["", "## Suite", "", describe(trajectories), "## Invariants "
          "(each must be 0)", ""]
    for k in inv_names:
        L.append(f"- **{k}: {inv_tot[k]}**")
    if not inv_names:
        L.append("- (no invariants recorded)")
    L += ["", "## Primary metrics (per-trajectory)", ""]
    for key, label in PRIMARY:
        L.append(f"- {label}: " + _stats([_metric(mets[i], key) for i in ids]))
    useful = {i: _useful(sealed[i]) for i in ids}
    L.append("- tasks useful: " + _stats([useful[i][0] for i in ids])
             + f" (of {sum(useful[i][1] for i in ids)} task steps)")
    per_task: dict[str, list[int]] = {}
    for i in ids:
        for tid, ev in (sealed[i].get("evaluations") or {}).items():
            c = per_task.setdefault(tid, [0, 0])
            c[1] += 1
            c[0] += ev.get("outcome") == "SUCCESS"
    L += ["", "## Useful transitions per task", ""]
    for tid in sorted(per_task):
        L.append(f"- {tid}: {per_task[tid][0]}/{per_task[tid][1]}")
    skip = {k for names in _ALIASES.values() for k in names} | {
        "recovery", "recovery_successes", "recovery_opportunities",
        "recoveries"}
    friction = sorted({k for m in mets.values() for k, v in m.items()
                       if k not in skip and isinstance(v, (int, float))
                       and not isinstance(v, bool)})
    L += ["", "## Friction totals", ""]
    for k in friction:
        L.append(f"- {k}: {sum(mets[i].get(k, 0) or 0 for i in ids)}")
    if not friction:
        L.append("- (none)")
    L += ["", "## Receipt-completeness disagreements", ""]
    n_dis = 0
    for i in ids:
        for d in sealed[i].get("disagreements") or []:
            rid = d.get("receipt_id") if isinstance(d, dict) else d
            L.append(f"- {i} {rid}"
                     + (f": {json.dumps(d, sort_keys=True)}"
                        if isinstance(d, dict) else ""))
            n_dis += 1
    if not n_dis:
        L.append("- none")
    # agent behaviour under lifecycle pressure, from the sealed receipts
    try:
        from blackwall.lifecycle_behavior import (aggregate_behaviour,
                                                  render_behaviour,
                                                  trajectory_behaviour)
        behaviours = []
        for i in ids:
            rpath = os.path.join(out, i, "receipts.jsonl")
            if os.path.exists(rpath):
                with open(rpath) as fh:
                    rows = [json.loads(line) for line in fh if line.strip()]
                behaviours.append(trajectory_behaviour(rows, sealed[i].get("evaluations")))
        if behaviours:
            # single-task baseline: phase 5 control, 16.8 mints / 12 tasks
            L += ["", render_behaviour(aggregate_behaviour(behaviours), 1.40).rstrip()]
            summary_behaviour = aggregate_behaviour(behaviours)
        else:
            summary_behaviour = None
    except ImportError:
        summary_behaviour = None
    L += ["", "## Per-trajectory", "",
          "| id | useful | stale | replay | wrong-scope | forged | "
          "cross-task | dup acq | dup eff | recovery | restarts | "
          "transport losses |", "|" + "---|" * 12]
    for i in ids:
        m = mets[i]
        rn, rm = _recovery(m)
        L.append(f"| {i} | {useful[i][0]}/{useful[i][1]} | "
                 + " | ".join(str(_metric(m, k)) for k in (
                     "stale", "replay", "wrong_scope", "forged", "cross_task",
                     "duplicate_acquisitions", "duplicate_effects"))
                 + f" | {rn}/{rm} | {_metric(m, 'restarts')} | "
                 f"{_metric(m, 'transport_losses')} |")
    for i in missing:
        L.append(f"| {i} | MISSING |" + " |" * 10)
    summary = {"invariants": inv_tot, "sealed": len(ids),
               "behaviour": summary_behaviour,
               "total": len(trajectories), "missing": missing,
               "aggregate": agg}
    return "\n".join(L) + "\n", summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True)
    p.add_argument("--trajectories", type=int, default=20)
    p.add_argument("--condition", choices=("control", "global", "denial",
                                           "task"), default="control")
    p.add_argument("--driver", choices=("llm", "scripted"), default="llm")
    p.add_argument("--attempts", type=int, default=8)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--model", default=os.environ.get("BLACKWALL_LLM_MODEL"))
    p.add_argument("--report", action="store_true")
    p.add_argument("--context", choices=("session", "reset"), default="session",
                   help="session: one conversation across the trajectory "
                        "(phase 5B); reset: the agent-facing conversation is "
                        "reset at every task transition (phase 5C-A)")
    p.add_argument("--suite", choices=("lifecycle", "expiry"), default="lifecycle",
                   help="lifecycle: phase 5B injections; expiry: phase 5C-B "
                        "forced mid-task expiry")
    a = p.parse_args(argv)
    if _is_canonical(a.out):
        print("error: refusing to write into results/canonical",
              file=sys.stderr)
        return 2

    from blackwall.lifecycle_suite import (describe, llm_trajectories,
                                           scripted_probe_trajectories)
    if a.driver == "scripted":
        from blackwall.lifecycle import probe_trajectories
        trajs = probe_trajectories()
    elif a.suite == "expiry":
        from blackwall.lifecycle_suite import expiry_trajectories
        trajs = expiry_trajectories(a.trajectories)
    else:
        trajs = llm_trajectories(a.trajectories)
    os.makedirs(a.out, exist_ok=True)

    if a.report:
        text, summary = build_report(a.out, trajs)
        with open(os.path.join(a.out, "report.md"), "w") as fh:
            fh.write(text)
        if summary["aggregate"] is not None:
            _write_json_atomic(os.path.join(a.out, "aggregate.json"),
                               summary["aggregate"])
        print("invariants: " + json.dumps(summary["invariants"],
                                          sort_keys=True))
        print(f"sealed {summary['sealed']}/{summary['total']}"
              + (f"; missing: {', '.join(summary['missing'])}"
                 if summary["missing"] else ""))
        print(f"wrote {a.out}/report.md")
        return 0

    from blackwall.llm_experiment import CONDITION_MODES
    mode = CONDITION_MODES[a.condition]
    base_url = os.environ.get("BLACKWALL_LLM_BASE_URL", "")
    model = a.model or "scripted"
    if a.driver == "llm":
        from blackwall.agent.drivers import LLMDriver
        if LLMDriver.from_env() is None:
            print("error: set BLACKWALL_LLM_BASE_URL (and "
                  "BLACKWALL_LLM_API_KEY for non-loopback endpoints); "
                  "refusing to run unconfigured", file=sys.stderr)
            return 2
        model = a.model or LLMDriver.from_env()._model
        api_key = os.environ.get("BLACKWALL_LLM_API_KEY", "")

        def factory(_traj):
            return LLMDriver(base_url, api_key, model,
                             discovery=mode or False,
                             session=(a.context == "session"))
    else:
        try:
            from blackwall.lifecycle import scripted_driver_for
        except ImportError:
            print("skip: blackwall.lifecycle.scripted_driver_for is not "
                  "available", file=sys.stderr)
            return 0

        def factory(traj):
            return scripted_driver_for(traj)

    meta = {"label": os.path.basename(os.path.normpath(a.out)),
            "model": model, "base_url": base_url, "condition": a.condition,
            "driver": a.driver, "trajectories": len(trajs),
            "context": a.context, "suite": a.suite,
            "started": datetime.now(timezone.utc).isoformat(),
            "git_revision": _git_revision()}
    with open(os.path.join(a.out, "suite.md"), "w") as fh:
        fh.write(describe(trajs))
    _write_json_atomic(os.path.join(a.out, "meta.json"), meta)
    ok = run_suite(trajs, a.out, factory, mode, a.attempts, a.max_steps)
    meta["finished"] = datetime.now(timezone.utc).isoformat()
    meta["complete"] = ok
    _write_json_atomic(os.path.join(a.out, "meta.json"), meta)
    if not ok:
        print("INCOMPLETE: endpoint failures exhausted --attempts",
              file=sys.stderr)
        return 1
    print(f"wrote {a.out}/ (run with --report for the summary)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
