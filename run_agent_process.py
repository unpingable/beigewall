#!/usr/bin/env python3
"""Untrusted agent process (phase 6).

Runs the fixed tasks through the real-model or scripted driver against a
trusted membrane reached only over `blackwall-ipc/1`. Receives a socket
path, a condition, a driver name and task ids — never a ledger, corpus or
policy path. Imports no trusted module. Emits one JSON telemetry line per
task on stdout.

  python3 run_agent_process.py --socket <path> --condition control \
      --driver llm|scripted --tasks t1-summarize-alice,t3-note-followup
"""

import argparse
import json
import os
import sys

from blackwall.ipc.client import RemoteBroker

CONDITION_MODES = {"control": None, "global": "global", "denial": "denial", "task": "task"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", required=True)
    p.add_argument("--condition", default="control", choices=sorted(CONDITION_MODES))
    p.add_argument("--driver", default="scripted", choices=("llm", "scripted"))
    p.add_argument("--tasks", required=True, help="comma-separated task ids")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--session", action="store_true",
                   help="llm: keep one conversation across tasks (default: reset per task)")
    a = p.parse_args()
    mode = CONDITION_MODES[a.condition]
    task_ids = [t for t in a.tasks.split(",") if t]

    from blackwall.ipc.agent import RemoteHarness
    broker = RemoteBroker(a.socket)
    harness = RemoteHarness(broker, discovery_mode=mode)

    if a.driver == "llm":
        from blackwall.agent.drivers import LLMDriver
        base = os.environ.get("BLACKWALL_LLM_BASE_URL")
        if not base:
            print("error: set BLACKWALL_LLM_BASE_URL/BLACKWALL_LLM_MODEL", file=sys.stderr)
            return 2
        shared = LLMDriver(base, os.environ.get("BLACKWALL_LLM_API_KEY", ""),
                           os.environ.get("BLACKWALL_LLM_MODEL", "local"),
                           discovery=mode or False, session=a.session)

        def make_driver(_tid):
            return shared if a.session else LLMDriver(
                base, os.environ.get("BLACKWALL_LLM_API_KEY", ""),
                os.environ.get("BLACKWALL_LLM_MODEL", "local"), discovery=mode or False)
    else:
        # the scripted driver's scripts are task definitions; the agent process
        # holds them only as a deterministic self-check reference (they carry
        # no authority and no corpus content beyond the public scripts)
        from blackwall.agent.drivers import ScriptedDriver
        from blackwall.tasks_public import task_scripts
        scripts = task_scripts()

        def make_driver(tid):
            s = scripts[tid]
            return ScriptedDriver(s["script"], s["narrative"])

    for tid in task_ids:
        driver = make_driver(tid)
        before = broker.telemetry()
        res = harness.run_task(tid, driver, max_steps=a.max_steps)
        after = broker.telemetry()
        ipc = {"requests": after["requests"] - before["requests"],
               "latency_ms": after["latency_ms"][len(before["latency_ms"]):],
               "bytes_in": after["bytes_in"] - before["bytes_in"],
               "bytes_out": after["bytes_out"] - before["bytes_out"],
               "reconnects": after["reconnects"] - before["reconnects"],
               "transport_failures": after["transport_failures"] - before["transport_failures"]}
        tele = driver.telemetry() if hasattr(driver, "telemetry") else {}
        tele["latency_total"] = sum(tele.get("latencies", []))
        line = {"task_id": tid, "answer_len": len(res["answer"]), "truncated": res["truncated"],
                **{k: v for k, v in tele.items() if k != "latencies"},
                "latencies": tele.get("latencies", []), "ipc": ipc}
        print(json.dumps(line, sort_keys=True), flush=True)
    broker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
