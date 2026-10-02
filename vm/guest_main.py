#!/usr/bin/env python3
"""Phase 7 guest entrypoint (runs as the only workload inside the VM).

Reads a base64 JSON config from the kernel command line (`bw.cfg=...`) and
dispatches:

* mode "probe": run the OS-boundary probe suite (probe_os_boundary) and emit
  its BW-PROBE-RESULT line to the console.
* mode "agent": run the UNMODIFIED untrusted agent (run_agent_process) against
  the host authority channel over vsock; for the llm driver, first start the
  loopback->vsock model forwarder and point the driver at it.
* mode "both": probe first, then agent.

Everything printed goes to the serial console, which the host captures. The
console is a one-way, guest-controlled advisory channel (like the
self-reported telemetry in phase 6); it is not an authority channel.
"""

import base64
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # /opt/bw
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)


def _cmdline_cfg() -> dict:
    with open("/proc/cmdline") as fh:
        parts = fh.read().split()
    for p in parts:
        if p.startswith("bw.cfg="):
            raw = base64.b64decode(p[len("bw.cfg="):])
            return json.loads(raw)
    raise SystemExit("BW-GUEST: no bw.cfg on the kernel command line")


def run_probe(cfg: dict) -> None:
    import probe_os_boundary
    pcfg = dict(cfg.get("probe", {}))
    pcfg.setdefault("host_cid", cfg.get("host_cid", 2))
    pcfg.setdefault("auth_port", cfg.get("auth_port"))
    pcfg.setdefault("relay_port", cfg.get("relay_port"))
    pcfg.setdefault("model", cfg.get("model", ""))
    probe_os_boundary.main_with_cfg(pcfg)


def start_forwarder(cfg: dict) -> int:
    import guest_model_forwarder as fwd
    listen = int(cfg.get("listen_port", 8080))
    host_cid = int(cfg.get("host_cid", 2))
    relay = int(cfg["relay_port"])

    def serve():
        fwd.main(["--listen-port", str(listen), "--host-cid", str(host_cid),
                  "--relay-port", str(relay)])

    threading.Thread(target=serve, daemon=True).start()
    return listen


def run_agent(cfg: dict) -> None:
    host_cid = int(cfg.get("host_cid", 2))
    auth = int(cfg["auth_port"])
    driver = cfg.get("driver", "scripted")
    if driver == "llm":
        listen = start_forwarder(cfg)
        import time
        time.sleep(0.5)
        os.environ["BLACKWALL_LLM_BASE_URL"] = f"http://127.0.0.1:{listen}/v1"
        os.environ["BLACKWALL_LLM_MODEL"] = cfg.get("model", "local")
        os.environ.setdefault("BLACKWALL_LLM_API_KEY", "")
    sys.argv = ["run_agent_process.py", "--socket", f"vsock:{host_cid}:{auth}",
                "--condition", cfg.get("condition", "control"),
                "--driver", driver, "--tasks", ",".join(cfg["tasks"]),
                "--max-steps", str(cfg.get("max_steps", 40))]
    import run_agent_process
    run_agent_process.main()


def main() -> int:
    cfg = _cmdline_cfg()
    mode = cfg.get("mode", "probe")
    print(f"BW-GUEST: mode={mode} host_cid={cfg.get('host_cid')} "
          f"auth_port={cfg.get('auth_port')} relay_port={cfg.get('relay_port')}",
          flush=True)
    if mode in ("probe", "both"):
        run_probe(cfg)
    if mode in ("agent", "both"):
        run_agent(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
