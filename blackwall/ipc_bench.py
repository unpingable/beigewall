"""Phase 6 micro-benchmark: cost of the process boundary.

Measures per-call latency (median and p95, milliseconds) of request_grant,
read (conversation@1 on thr-alice) and effect (send_message on thr-alice
with valid provenance) for the in-process broker and, when a socket path is
given, for the RemoteBroker over the `blackwall-ipc/1` socket. Also reports
the read response size in bytes and the connect cost (open + hello + close).

Effects consume their grant, so every timed effect gets a fresh grant minted
outside the timed region. Timing uses time.perf_counter only; the call
sequence is deterministic. The IPC modules are imported lazily so that the
in-process half works without them.

Usage:
  python3 -m blackwall.ipc_bench [--n 200] [--out results/exploratory/x.json]
"""

import argparse
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

TASK_ID = "t8-send-reply"
THREAD = "messages/thread/thr-alice"
PROJECTION = "conversation@1"


def _task():
    from .tasks import task_suite
    return next(t for t in task_suite() if t.task_id == TASK_ID)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def _summ(samples_s: list[float]) -> dict:
    ms = [s * 1000.0 for s in samples_s]
    return {"median_ms": _percentile(ms, 0.5), "p95_ms": _percentile(ms, 0.95),
            "n": len(ms)}


class _InProcess:
    """Adapter over CapabilityBroker with the same four calls as the remote."""

    def __init__(self):
        from .authority import PolicyAuthority
        from .broker import CapabilityBroker
        from .corpus import build_demo_corpus
        from .receipts import Ledger, canonical_hash
        from .tasks import LogicalClock, experiment_policy
        clock = LogicalClock()
        self.ledger = Ledger(clock=clock.now)
        self.broker = CapabilityBroker(
            build_demo_corpus(), PolicyAuthority(experiment_policy(),
                                                 clock=clock.now),
            self.ledger, clock=clock.now)
        self.prompt_hash = canonical_hash({"prompt": _task().prompt})
        self.broker.begin_task(TASK_ID, self.prompt_hash)

    def request_grant(self, resource, operation, projection=None):
        return self.broker.request_grant(TASK_ID, resource, operation,
                                         projection=projection)

    def read(self, grant_id):
        return self.broker.read(TASK_ID, grant_id, THREAD, PROJECTION)

    def effect(self, grant_id, derived_from):
        return self.broker.effect(
            TASK_ID, grant_id, "send_message",
            {"thread_id": "thr-alice", "body": "bench"}, derived_from)

    def close(self):
        pass


class _Remote:
    def __init__(self, socket_path):
        from .ipc.client import RemoteBroker
        self.rb = RemoteBroker(socket_path)
        info = self.rb.begin_task(TASK_ID)
        self.prompt_hash = info["prompt_hash"]

    def request_grant(self, resource, operation, projection=None):
        return self.rb.request_grant(TASK_ID, resource, operation,
                                     projection=projection)

    def read(self, grant_id):
        return self.rb.read(TASK_ID, grant_id, THREAD, PROJECTION)

    def effect(self, grant_id, derived_from):
        return self.rb.effect(
            TASK_ID, grant_id, "send_message",
            {"thread_id": "thr-alice", "body": "bench"}, derived_from)

    def close(self):
        self.rb.close()


def _response_bytes(result: dict) -> int:
    try:
        from .ipc.protocol import encode_frame, ok_response
        return len(encode_frame(ok_response("bench", result)))
    except Exception:
        return len(json.dumps(result, sort_keys=True).encode("utf-8")) + 4


def _bench_one(side, n: int) -> dict:
    pc = time.perf_counter
    grant_t, read_t, effect_t = [], [], []
    # request_grant: timed, n grants
    for _ in range(n):
        t0 = pc()
        side.request_grant(THREAD, "read", PROJECTION)
        grant_t.append(pc() - t0)
    # read: reuse a grant until its budget is spent (mint outside the timer)
    g, left = None, 0
    size = 0
    for _ in range(n):
        if left <= 0:
            res = side.request_grant(THREAD, "read", PROJECTION)
            g, left = res["grant_id"], res["budget"]["reads"]
        t0 = pc()
        out = side.read(g)
        read_t.append(pc() - t0)
        left -= 1
        size = _response_bytes(out)
    # effect: one fresh grant each; provenance from the last read
    read_out = side.read(side.request_grant(THREAD, "read",
                                            PROJECTION)["grant_id"])
    derived = [{"type": "projection",
                "ref": read_out["projection_meta"]["content_hash"]},
               {"type": "user_input", "ref": side.prompt_hash}]
    for _ in range(n):
        gid = side.request_grant(THREAD, "send_message")["grant_id"]
        t0 = pc()
        side.effect(gid, derived)
        effect_t.append(pc() - t0)
    return {"request_grant": _summ(grant_t), "read": _summ(read_t),
            "effect": _summ(effect_t), "read_response_bytes": size}


def _connect_cost(socket_path: str, n: int) -> dict:
    from .ipc.protocol import (VERSION, encode_frame, read_frame)
    samples = []
    for i in range(n):
        t0 = time.perf_counter()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(socket_path)
        s.sendall(encode_frame({"v": VERSION, "id": f"c{i}", "op": "hello",
                                "task_id": TASK_ID, "args": {}}))
        read_frame(s)
        s.close()
        samples.append(time.perf_counter() - t0)
    return _summ(samples)


def bench(socket_path: str | None = None, n: int = 200) -> dict:
    """Return {"n", "in_process": {...}, "ipc": {...} | None}. Each side runs
    against its own fresh state; the IPC side needs a live server."""
    side = _InProcess()
    result = {"n": n, "task_id": TASK_ID, "in_process": _bench_one(side, n),
              "ipc": None}
    if socket_path is not None:
        remote = _Remote(socket_path)
        try:
            ipc = _bench_one(remote, n)
        finally:
            remote.close()
        ipc["connect"] = _connect_cost(socket_path, min(n, 50))
        result["ipc"] = ipc
    return result


def render_table(result: dict) -> str:
    lines = ["| call | in-process median ms | in-process p95 ms | "
             "ipc median ms | ipc p95 ms |", "|---|---|---|---|---|"]
    ip, ipc = result["in_process"], result["ipc"]
    for call in ("request_grant", "read", "effect"):
        row = [f"{ip[call]['median_ms']:.4f}", f"{ip[call]['p95_ms']:.4f}"]
        row += ([f"{ipc[call]['median_ms']:.4f}", f"{ipc[call]['p95_ms']:.4f}"]
                if ipc else ["n/a", "n/a"])
        lines.append(f"| {call} | " + " | ".join(row) + " |")
    lines.append("")
    lines.append(f"read response size: {ip['read_response_bytes']} bytes "
                 f"(framed: "
                 f"{ipc['read_response_bytes'] if ipc else 'n/a'})")
    if ipc:
        c = ipc["connect"]
        lines.append(f"connect (open+hello+close): median "
                     f"{c['median_ms']:.4f} ms, p95 {c['p95_ms']:.4f} ms")
    lines.append(f"n = {result['n']} calls per measurement")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--out", default=None, help="write JSON results here")
    a = p.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp(prefix="bw-bench-")
    os.chmod(tmp, 0o700)
    sock_dir = os.path.join(tmp, "sock")
    os.mkdir(sock_dir, 0o700)
    sock = os.path.join(sock_dir, "s")
    server = subprocess.Popen(
        [sys.executable, "-m", "blackwall.ipc.server", "--socket", sock,
         "--ledger", os.path.join(tmp, "receipts.jsonl"),
         "--condition", "control", "--run-id", "bench"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while not os.path.exists(sock) and time.time() < deadline:
            time.sleep(0.05)
        if not os.path.exists(sock):
            print("error: server socket did not appear", file=sys.stderr)
            return 1
        result = bench(sock, a.n)
    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print(render_table(result))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
