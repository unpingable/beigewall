#!/usr/bin/env python3
"""Phase 7 orchestrator: the BeigeWall boundary across a VM/OS boundary.

The trusted membrane (corpus, authority, broker, projections, effects,
ledger) runs on the HOST and listens on a vsock port for exactly one guest
CID. The untrusted agent runs INSIDE a QEMU/KVM guest that has no disk, no
shared filesystem and no network device — only two vsock channels to the
host: the authority channel (the trusted server) and a separate model relay
(host LLM transport only). The guest is treated as fully compromised.

Three things can run against one booted guest:
  --probe                 the OS-boundary probe suite (probe_os_boundary)
  --driver scripted       the deterministic agent (normative self-check);
                          its host ledger is evaluated like phase 6
  --driver llm            the real-model agent through the relay

The host collects the guest's serial console (advisory, guest-controlled)
and, for an agent run, evaluates the HOST ledger with the unchanged
evaluator — that ledger, not the console, is the trusted evidence.

Example (deterministic, with probe):
  python3 run_vm_experiment.py --probe --driver scripted --condition control \
      --kernel <vmlinuz> --initrd <initramfs.gz> --out results/exploratory/phase7-...
"""

import argparse
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
PROBE_SENTINEL = "BW-PROBE-RESULT: "
VSOCK_HOST_CID = 2


def _free_vsock_port(preferred: int) -> int:
    """A host vsock port nothing is listening on (bind test on CID_ANY)."""
    for port in range(preferred, preferred + 200):
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        try:
            s.bind((socket.VMADDR_CID_ANY, port))
            s.close()
            return port
        except OSError:
            s.close()
    raise RuntimeError("no free vsock port")


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def probe_config(cfg: dict) -> dict:
    """What the in-guest probe genuinely attempts. Host paths are REAL host
    paths (so an ENOENT in the guest is isolation, not a missing attempt)."""
    auth, relay = cfg["auth_port"], cfg["relay_port"]
    return {
        "host_cid": VSOCK_HOST_CID, "auth_port": auth, "relay_port": relay,
        "model": cfg.get("model", ""),
        "probe": {
            "host_paths": [
                cfg["ledger_path"],
                os.path.join(ROOT, "blackwall", "broker.py"),
                os.path.join(ROOT, "blackwall", "corpus.py"),
                os.path.join(ROOT, "results", "canonical", "receipts.jsonl"),
                "/etc/passwd", "/etc/shadow",
                os.path.expanduser("~/.ssh/id_rsa"),
            ],
            "host_modules_dir": ROOT,
            # intended ports + a spread of unintended ones (incl. neighbours)
            "scan_ports": sorted({auth, relay, 22, 1024, 5000, 11434,
                                  auth + 1, relay + 1, auth - 1}),
            "scan_cids": [0, 1, 3, VSOCK_HOST_CID + 1],
        },
    }


def guest_config(cfg: dict, probe: bool, driver: str) -> dict:
    mode = ("both" if probe and driver != "none"
            else "probe" if probe else "agent")
    g = {"mode": mode, "host_cid": VSOCK_HOST_CID,
         "auth_port": cfg["auth_port"], "relay_port": cfg["relay_port"],
         "listen_port": 8080, "condition": cfg["condition"],
         "driver": driver, "tasks": cfg["tasks"], "model": cfg.get("model", ""),
         "max_steps": cfg["max_steps"]}
    if probe:
        g["probe"] = probe_config(cfg)["probe"]
    return g


def start_server(run_dir, auth_port, guest_cid, condition, task_ids, run_id):
    ledger = os.path.join(run_dir, "receipts.jsonl")
    log = open(os.path.join(run_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "blackwall.ipc.server",
         "--vsock-port", str(auth_port), "--vsock-peer-cid", str(guest_cid),
         "--ledger", ledger, "--condition", condition, "--run-id", run_id,
         "--tasks", ",".join(task_ids)],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    return proc, log, ledger


def start_relay(run_dir, relay_port, guest_cid, upstream, model):
    log_path = os.path.join(run_dir, "relay.jsonl")
    proc = subprocess.Popen(
        [sys.executable, "-m", "blackwall.model_relay",
         "--vsock-port", str(relay_port), "--peer-cid", str(guest_cid),
         "--upstream", upstream, "--model", model, "--log", log_path],
        cwd=ROOT, stdout=subprocess.DEVNULL,
        stderr=open(os.path.join(run_dir, "relay.err"), "w"))
    return proc, log_path


def boot_guest(run_dir, cfg, guest_cfg, guest_cid, timeout):
    b64 = base64.b64encode(json.dumps(guest_cfg).encode()).decode()
    append = f"console=ttyS0 panic=-1 rdinit=/init bw.cfg={b64}"
    cmd = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
           "-m", str(cfg["mem"]), "-smp", str(cfg["smp"]),
           "-kernel", cfg["kernel"], "-initrd", cfg["initrd"],
           "-append", append, "-nographic", "-no-reboot", "-nic", "none",
           "-device", f"vhost-vsock-pci,guest-cid={guest_cid}"]
    console = os.path.join(run_dir, "console.log")
    with open(console, "w") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            rc = proc.wait(timeout=timeout)
            killed = False
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(); rc, killed = -9, True
    return console, rc, killed


def parse_console(console_path):
    probe_result, telemetry, guest_lines = None, [], []
    with open(console_path, errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith(PROBE_SENTINEL):
                try:
                    probe_result = json.loads(line[len(PROBE_SENTINEL):])
                except json.JSONDecodeError:
                    pass
            elif line.startswith("BW-GUEST:"):
                guest_lines.append(line)
            else:
                s = line.strip()
                if s.startswith("{") and '"task_id"' in s:
                    try:
                        telemetry.append(json.loads(s))
                    except json.JSONDecodeError:
                        pass
    return probe_result, telemetry, guest_lines


def host_same_user_control(ledger_path):
    """The phase-6 contrast: a same-UID HOST process CAN read the ledger the
    guest could not — so the OS boundary, not a convention, is what stopped
    the guest."""
    try:
        with open(ledger_path, "rb") as fh:
            data = fh.read()
        import hashlib
        return {"host_can_read_ledger": True, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}
    except OSError as exc:
        return {"host_can_read_ledger": False, "error": str(exc)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kernel", required=True)
    p.add_argument("--initrd", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--driver", choices=("none", "scripted", "llm"), default="none")
    p.add_argument("--condition", default="control",
                   choices=("control", "global", "denial", "task"))
    p.add_argument("--tasks", default=None, help="comma-separated (default: full suite)")
    p.add_argument("--guest-cid", type=int, default=55)
    p.add_argument("--auth-port", type=int, default=9100)
    p.add_argument("--relay-port", type=int, default=9200)
    p.add_argument("--mem", type=int, default=2048)
    p.add_argument("--smp", type=int, default=2)
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--upstream", default=os.environ.get(
        "BLACKWALL_LLM_BASE_URL", "http://127.0.0.1:11434"))
    p.add_argument("--model", default=os.environ.get("BLACKWALL_LLM_MODEL", "local"))
    a = p.parse_args(argv)
    if not a.probe and a.driver == "none":
        p.error("nothing to do: pass --probe and/or --driver")

    from blackwall.tasks import task_suite
    tasks = task_suite()
    task_ids = ([t for t in a.tasks.split(",") if t] if a.tasks
                else [t.task_id for t in tasks])

    out = os.path.abspath(a.out)
    canonical = os.path.realpath(os.path.join(ROOT, "results", "canonical"))
    if os.path.realpath(out) == canonical or os.path.realpath(out).startswith(canonical + os.sep):
        p.error("refusing to write into results/canonical")
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)

    auth_port = _free_vsock_port(a.auth_port)
    relay_port = _free_vsock_port(a.relay_port)
    # upstream for the relay: strip a trailing /v1 (relay appends the path)
    upstream = a.upstream[:-3] if a.upstream.rstrip("/").endswith("/v1") else a.upstream
    upstream = upstream.rstrip("/")
    run_id = f"phase7-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    cfg = {"kernel": os.path.abspath(a.kernel), "initrd": os.path.abspath(a.initrd),
           "auth_port": auth_port, "relay_port": relay_port,
           "condition": a.condition, "tasks": task_ids, "model": a.model,
           "max_steps": a.max_steps, "mem": a.mem, "smp": a.smp}

    server, slog, ledger = start_server(out, auth_port, a.guest_cid,
                                        a.condition, task_ids, run_id)
    cfg["ledger_path"] = ledger
    relay = relay_log = None
    if a.driver == "llm" or a.probe:
        relay, relay_log = start_relay(out, relay_port, a.guest_cid,
                                       upstream, a.model)
    time.sleep(1.0)   # let both listeners bind before the guest dials
    if server.poll() is not None:
        print("error: trusted server exited early; see server.log", file=sys.stderr)
        return 2

    gcfg = guest_config(cfg, a.probe, a.driver)
    print(f"booting guest cid={a.guest_cid} auth=vsock:2:{auth_port} "
          f"relay=vsock:2:{relay_port} mode={gcfg['mode']}", flush=True)
    console, rc, killed = boot_guest(out, cfg, gcfg, a.guest_cid, a.timeout)

    for proc in (server, relay):
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in (server, relay):
        if proc:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
    slog.close()

    probe_result, telemetry, guest_lines = parse_console(console)
    result = {"run_id": run_id, "condition": a.condition, "driver": a.driver,
              "probe": a.probe, "guest_cid": a.guest_cid,
              "auth_port": auth_port, "relay_port": relay_port,
              "qemu_rc": rc, "qemu_killed": killed,
              "guest_lines": guest_lines,
              "server_rc": server.returncode,
              "relay_rc": relay.returncode if relay else None,
              "same_user_control": host_same_user_control(ledger),
              "architecture": "vm-separated", "vm": "qemu/kvm vhost-vsock"}

    if probe_result is not None:
        _write_json(os.path.join(out, "probe_result.json"), probe_result)
        result["probe_summary"] = probe_result["summary"]

    if a.driver != "none":
        from run_ipc_experiment import evaluate_ledger
        tele_by_task = {t["task_id"]: t for t in telemetry if "task_id" in t}
        try:
            with open(ledger) as fh:
                has_rows = any(line.strip() for line in fh)
        except OSError:
            has_rows = False
        if has_rows:
            rec = evaluate_ledger(ledger, tele_by_task, tasks, run_id=run_id,
                                  label="phase7", model=a.model,
                                  condition=a.condition, driver=a.driver,
                                  revision=_git_rev())
            _write_json(os.path.join(out, "metrics.json"), rec)
            result["suite"] = rec["suite"]
            result["ipc"] = rec["ipc"]
        else:
            result["error"] = "no ledger rows (agent never reached the server)"

    _write_json(os.path.join(out, "result.json"), result)
    print(f"wrote {out}/result.json (qemu rc={rc}, killed={killed})")
    if probe_result:
        s = probe_result["summary"]
        print(f"probe: {s['total']} checks, reached_trusted_state="
              f"{s['reached_trusted_state']} {s['reached_names']}, "
              f"mechanism_ok={s['mechanism_ok']}")
    if "suite" in result:
        rg = result["suite"].get("receipt_grounded", {})
        print(f"agent: receipt-grounded success {rg.get('succeeded')}/"
              f"{len(task_ids)} (inadmissible {rg.get('inadmissible')}, "
              f"unknown {rg.get('unknown')})")
    return 0


def _git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=ROOT, timeout=5).stdout.strip()
    except Exception:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
