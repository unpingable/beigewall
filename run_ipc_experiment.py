#!/usr/bin/env python3
"""Phase 6 trusted-side orchestrator: the task suite across a process boundary.

For each run it creates a private run directory (mode 0700) holding the
receipt ledger and a private socket directory, starts the trusted server
(`python3 -m beigewall.ipc.server`), then starts the untrusted agent
(`run_agent_process.py`) with only --socket, --condition, --driver and
--tasks. The agent is never given the ledger path, corpus or policy. After
the agent exits the server is stopped with SIGTERM and the ledger is
evaluated with the unchanged evaluator and metrics, exactly as the
in-process runner does. See docs/PROCESS_BOUNDARY.md.

Usage:
  python3 run_ipc_experiment.py --runs 3 --label scripted --condition control \
      --driver scripted
  BEIGEWALL_LLM_BASE_URL=... BEIGEWALL_LLM_MODEL=... \
  python3 run_ipc_experiment.py --runs 5 --label qwen --condition global \
      --driver llm [--out results/exploratory/phase6-ipc-<label>-<utc>]

Canonical results are never written. Runs whose metrics.json parses and
holds all 12 tasks are reused; an agent crash is recorded and the run is
left unsealed.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CONDITIONS = ("control", "global", "denial", "task")
SOCKET_WAIT = 10.0
SERVER_STOP_WAIT = 15.0


def _git_revision() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=ROOT, timeout=5).stdout.strip()
    except Exception:
        return "unknown"


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _load_sealed(path: str, n_tasks: int) -> dict | None:
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if len(rec.get("per_task", {})) != n_tasks:
        return None
    return rec


def _read_telemetry(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and "task_id" in obj:
                    out[obj["task_id"]] = obj
    except OSError:
        pass
    return out


def evaluate_ledger(receipts_path: str, telemetry: dict[str, dict], tasks,
                    *, run_id: str, label: str, model: str, condition: str,
                    driver: str, revision: str) -> dict:
    """Build the metrics record from the trusted ledger alone (plus the
    advisory agent telemetry), the way run_llm_experiment does."""
    from beigewall.evaluator import evaluate_task
    from beigewall.metrics import suite_metrics, task_metrics
    from beigewall.phase4 import _ledger_from_rows, classify
    with open(receipts_path) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    ledger = _ledger_from_rows(rows)
    per_task: dict[str, dict] = {}
    success: dict[str, bool] = {}
    ipc_total = Counter()
    latencies: list[float] = []
    for task in tasks:
        ev = evaluate_task(rows, task.task_id, task.prompt)
        ok = ev["outcome"] == "SUCCESS"
        success[task.task_id] = ok
        record = task_metrics(ledger, task.task_id, ok)
        tele = telemetry.get(task.task_id, {})
        ipc = dict(tele.get("ipc") or {})
        record["telemetry"] = tele
        record["ipc"] = ipc
        record["receipt_grounded"] = ev
        record["class"] = classify(ev)
        per_task[task.task_id] = record
        for k in ("requests", "bytes_in", "bytes_out", "reconnects",
                  "transport_failures"):
            v = ipc.get(k, 0)
            ipc_total[k] += v if isinstance(v, int) and not isinstance(v, bool) else 0
        lat = ipc.get("latency_ms") or []
        latencies.extend(float(x) for x in (lat if isinstance(lat, list) else [])
                         if isinstance(x, (int, float)) and not isinstance(x, bool))
    suite = suite_metrics(per_task, success)
    suite["receipt_grounded"] = {
        "succeeded": sum(1 for r in per_task.values()
                         if r["receipt_grounded"]["outcome"] == "SUCCESS"),
        "unknown": sum(1 for r in per_task.values()
                       if r["receipt_grounded"]["outcome"] == "UNKNOWN"),
        "inadmissible": sum(
            1 for r in per_task.values()
            if r["receipt_grounded"]["admissibility"]["verdict"]
            == "INADMISSIBLE"),
    }
    ipc_run = dict(ipc_total)
    ipc_run["mean_latency_ms"] = (sum(latencies) / len(latencies)
                                  if latencies else None)
    ipc_run["bytes_per_task"] = ((ipc_total["bytes_in"] + ipc_total["bytes_out"])
                                 / len(tasks) if tasks else None)
    return {"run_id": run_id, "label": label, "model": model,
            "condition": condition, "driver": driver,
            "architecture": "process-separated",
            "beigewall_revision": revision, "per_task": per_task,
            "suite": suite, "ipc": ipc_run}


def _policy_args(policy: dict | None) -> list[str]:
    flags = (("effect_count", "--effect-count"), ("task_effects", "--task-effects"),
             ("rate", "--rate"))
    out: list[str] = []
    for key, flag in flags:
        if policy and policy.get(key) is not None:
            out += [flag, str(policy[key])]
    return out


def execute_run(run_dir: str, *, condition: str, driver: str, task_ids: list[str],
                max_steps: int, run_id: str, agent_timeout: float,
                resource_policy: dict | None = None) -> dict:
    """Start server + agent, return {"agent_rc", "server_rc", "error"?}.
    Never raises on a subprocess failure; the caller decides about sealing."""
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    os.chmod(run_dir, 0o700)
    receipts = os.path.join(run_dir, "receipts.jsonl")
    sock_dir = os.path.join(run_dir, "sock")
    for stale in (receipts, os.path.join(run_dir, "agent_telemetry.jsonl")):
        if os.path.exists(stale):
            os.unlink(stale)
    shutil.rmtree(sock_dir, ignore_errors=True)
    os.mkdir(sock_dir, 0o700)
    sock = os.path.join(sock_dir, "bw.sock")
    if len(sock.encode()) > 100:
        raise ValueError(f"socket path too long for AF_UNIX: {sock!r}; "
                         "use a shorter --out")
    info: dict = {"agent_rc": None, "server_rc": None}
    server_log = open(os.path.join(run_dir, "server.log"), "w")
    server = subprocess.Popen(
        [sys.executable, "-m", "beigewall.ipc.server", "--socket", sock,
         "--ledger", receipts, "--condition", condition, "--run-id", run_id,
         "--tasks", ",".join(task_ids)] + _policy_args(resource_policy),
        cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + SOCKET_WAIT
        while not os.path.exists(sock):
            if server.poll() is not None or time.time() > deadline:
                info["error"] = ("server exited before socket appeared"
                                 if server.poll() is not None
                                 else "socket did not appear within 10 s")
                return info
            time.sleep(0.05)
        agent_cmd = [sys.executable, os.path.join(ROOT, "run_agent_process.py"),
                     "--socket", sock, "--condition", condition,
                     "--driver", driver, "--tasks", ",".join(task_ids),
                     "--max-steps", str(max_steps)]
        with open(os.path.join(run_dir, "agent_telemetry.jsonl"), "w") as tele, \
                open(os.path.join(run_dir, "agent_stderr.log"), "w") as err:
            # the agent process inherits only what it needs, not the
            # orchestrator's environment (credentials, agent sockets, ...)
            agent_env = {k: v for k, v in os.environ.items()
                         if k.startswith("BEIGEWALL_LLM_") or k in ("PATH", "HOME", "LANG",
                                                                     "LC_ALL", "PYTHONPATH")}
            agent = subprocess.Popen(agent_cmd, cwd=ROOT, stdout=tele,
                                     stderr=err, env=agent_env)
            try:
                info["agent_rc"] = agent.wait(timeout=agent_timeout)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait()
                info["agent_rc"] = -9
                info["error"] = f"agent timed out after {agent_timeout:.0f} s"
        if info["agent_rc"] != 0 and "error" not in info:
            info["error"] = f"agent exited {info['agent_rc']}"
        return info
    finally:
        if server.poll() is None:
            server.send_signal(signal.SIGTERM)
        try:
            info["server_rc"] = server.wait(timeout=SERVER_STOP_WAIT)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
            info["server_rc"] = -9
            info["server_killed"] = True
        server_log.close()


def _budget_section(meta: dict, records: list[dict], out: str | None) -> list[str]:
    from beigewall.budget_metrics import CLASSIFICATIONS, run_budget_summary
    lines = ["", "## Resource budgets", "",
             f"- configured policy: {meta.get('resource_policy')}", "",
             "| run | effects | budget refusals (count/task/rate) | "
             + " | ".join(CLASSIFICATIONS) + " |",
             "|---|---|---|" + "---|" * len(CLASSIFICATIONS)]
    for rec in records:
        i = rec["_index"]
        try:
            with open(os.path.join(out, f"run-{i}", "receipts.jsonl")) as fh:
                rows = [json.loads(l) for l in fh if l.strip()]
            s = run_budget_summary(rows)
        except Exception as exc:
            lines.append(f"| run-{i} | n/a ({type(exc).__name__}) |" + " |" * (1 + len(CLASSIFICATIONS)))
            continue
        t = s["totals"]["budget_refusals"]
        lines.append(f"| run-{i} | {s['totals']['effects']} | "
                     f"{t['total']} ({t['count']}/{t['task']}/{t['rate']}) | "
                     + " | ".join(str(s["classification"][c])
                                  for c in CLASSIFICATIONS) + " |")
    return lines


def _render_report(meta: dict, records: list[dict], infos: dict,
                   out: str | None = None) -> str:
    lines = [
        "# BeigeWall process-separated run (phase 6, exploratory)", "",
        f"- label: {meta['label']}", f"- model: {meta['model']}",
        f"- condition: {meta['condition']}", f"- driver: {meta['driver']}",
        f"- architecture: {meta['architecture']}",
        f"- runs: executed {meta['runs_executed']}, reused "
        f"{meta['runs_reused']}, crashed {meta['runs_crashed']}",
        f"- beigewall revision: {meta['beigewall_revision_at_start']}", "",
        "## Per run", "",
        "| run | success | outcome classes | mean IPC latency ms | "
        "bytes/task | reconnects | transport failures | server exit |",
        "|---|---|---|---|---|---|---|---|"]
    for rec in records:
        i = rec["_index"]
        classes = Counter(t["class"] for t in rec["per_task"].values())
        ipc = rec["ipc"]
        succ = sum(1 for t in rec["per_task"].values() if t["success"])
        lat = ipc.get("mean_latency_ms")
        bpt = ipc.get("bytes_per_task")
        src = infos.get(i, {}).get("server_rc", "n/a")
        lines.append(
            f"| run-{i} | {succ}/{len(rec['per_task'])} | "
            f"{dict(sorted(classes.items()))} | "
            f"{'n/a' if lat is None else '%.3f' % lat} | "
            f"{'n/a' if bpt is None else '%.0f' % bpt} | "
            f"{ipc.get('reconnects', 0)} | "
            f"{ipc.get('transport_failures', 0)} | {src} |")
    if out is not None:
        lines += _budget_section(meta, records, out)
    if meta["agent_crashes"]:
        lines += ["", "## Unsealed runs", ""]
        for c in meta["agent_crashes"]:
            lines.append(f"- run-{c['run']}: {c['error']} "
                         f"(agent rc {c.get('agent_rc')}, server rc "
                         f"{c.get('server_rc')})")
    lines += ["", "Server exit code 0 means a clean SIGTERM shutdown; "
              "agent telemetry is advisory (self-reported across the "
              "boundary).", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--condition", choices=CONDITIONS, default="control")
    p.add_argument("--driver", choices=("llm", "scripted"), required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--agent-timeout", type=float, default=3600.0)
    p.add_argument("--effect-count", default=None,
                   help="server --effect-count (per task, effect and target), passed through")
    p.add_argument("--task-effects", default=None,
                   help="server --task-effects (per-task effect budget), passed through")
    p.add_argument("--rate", default=None,
                   help="server --rate n/seconds (effect rate limit), passed through")
    a = p.parse_args(argv)

    model = (os.environ.get("BEIGEWALL_LLM_MODEL", "unset")
             if a.driver == "llm" else "scripted-driver")
    if a.driver == "llm" and not os.environ.get("BEIGEWALL_LLM_BASE_URL"):
        print("error: set BEIGEWALL_LLM_BASE_URL (and BEIGEWALL_LLM_MODEL) "
              "for --driver llm", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.abspath(a.out or os.path.join(
        ROOT, "results", "exploratory", f"phase6-ipc-{a.label}-{stamp}"))
    canonical = os.path.realpath(os.path.join(ROOT, "results", "canonical"))
    real = os.path.realpath(out)
    if real == canonical or real.startswith(canonical + os.sep):
        print("error: refusing to write into results/canonical", file=sys.stderr)
        return 2

    from beigewall.tasks import task_suite
    tasks = task_suite()
    task_ids = [t.task_id for t in tasks]
    os.makedirs(out, mode=0o700, exist_ok=True)
    meta_path = os.path.join(out, "meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as fh:
                prior = json.load(fh)
        except (OSError, json.JSONDecodeError):
            prior = {}
        policy_now = {"effect_count": a.effect_count,
                      "task_effects": a.task_effects, "rate": a.rate}
        for key, value in (("condition", a.condition), ("model", model),
                           ("driver", a.driver), ("resource_policy", policy_now)):
            if prior.get(key) not in (None, value):
                print(f"error: refusing to resume {out}: existing {key}="
                      f"{prior.get(key)!r} differs from {value!r}",
                      file=sys.stderr)
                return 2

    resource_policy = {"effect_count": a.effect_count,
                       "task_effects": a.task_effects, "rate": a.rate}
    started = datetime.now(timezone.utc).isoformat()
    revision = _git_revision()
    records: list[dict] = []
    infos: dict[int, dict] = {}
    reused: list[int] = []
    executed: list[int] = []
    crashes: list[dict] = []
    for i in range(1, a.runs + 1):
        run_dir = os.path.join(out, f"run-{i}")
        sealed = os.path.join(run_dir, "metrics.json")
        prior_rec = _load_sealed(sealed, len(tasks))
        if prior_rec is not None:
            prior_rec["_index"] = i
            records.append(prior_rec)
            try:
                with open(os.path.join(run_dir, "run_info.json")) as fh:
                    infos[i] = json.load(fh)
            except (OSError, json.JSONDecodeError):
                infos[i] = {}
            reused.append(i)
            continue
        executed.append(i)
        run_id = f"{a.label}-run-{i}"
        print(f"run {i}/{a.runs}: starting server + agent", flush=True)
        info = execute_run(run_dir, condition=a.condition, driver=a.driver,
                           task_ids=task_ids, max_steps=a.max_steps,
                           run_id=run_id, agent_timeout=a.agent_timeout,
                           resource_policy=resource_policy)
        infos[i] = info
        _write_json(os.path.join(run_dir, "run_info.json"), info)
        receipts = os.path.join(run_dir, "receipts.jsonl")
        if info.get("agent_rc") != 0 or not os.path.exists(receipts):
            crashes.append({"run": i, **info,
                            "error": info.get("error", "no ledger")})
            print(f"run {i}: not sealed ({crashes[-1]['error']})",
                  file=sys.stderr)
            continue
        telemetry = _read_telemetry(os.path.join(run_dir,
                                                 "agent_telemetry.jsonl"))
        try:
            rec = evaluate_ledger(receipts, telemetry, tasks, run_id=run_id,
                                  label=a.label, model=model,
                                  condition=a.condition, driver=a.driver,
                                  revision=revision)
        except Exception as exc:   # an agent can shape its records; never lose the run silently
            crashes.append({"run": i, "error": f"evaluation failed: {type(exc).__name__}: {exc}"})
            print(f"run {i}: evaluation failed ({exc})", flush=True)
            continue
        _write_json(sealed, rec)
        rec["_index"] = i
        records.append(rec)

    meta = {
        "label": a.label, "model": model, "condition": a.condition,
        "driver": a.driver, "runs": a.runs, "max_steps": a.max_steps,
        "architecture": "process-separated", "exploratory": True,
        "runs_reused": reused, "runs_executed": executed,
        "runs_crashed": [c["run"] for c in crashes],
        "agent_crashes": crashes,
        "resource_policy": resource_policy,
        "server_exit_codes": {str(i): infos[i].get("server_rc")
                              for i in sorted(infos)},
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "beigewall_revision_at_start": revision,
        "note": "exploratory; agent telemetry is advisory across the "
                "process boundary; no credentials recorded",
    }
    _write_json(meta_path, meta)
    with open(os.path.join(out, "report.md"), "w") as fh:
        fh.write(_render_report(meta, sorted(records, key=lambda r: r["_index"]),
                                infos, out))
    print(f"wrote {out}/ ({len(records)} sealed, {len(crashes)} unsealed)")
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
