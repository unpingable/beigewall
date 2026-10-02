"""Hostile effect-stress client (phase 6B, docs/RESOURCE_BOUNDS.md).

A compromised but correctly authorized client that tries to land more
`send_message` effects than the server's resource budget allows: fresh
grants, one grant reused, concurrent connections, reconnects, a second
(restarted) untrusted process, replayed frames, forged counters, forged
task and grant ids, and reordered frames.

Client-side outcomes are *observations by the attacker* only. Effects that
actually landed are counted from the trusted ledger (`count_landed`), by
the observer that started the server; the attacking code never reads it.

Like the agent process, this module imports nothing trusted: only the
protocol framing.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import Counter

from . import protocol as P

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TASK = "t8-send-reply"
THREAD = "thr-alice"
RESOURCE = f"messages/thread/{THREAD}"
EFFECT = "send_message"
PROJECTION = "conversation@1"


class Transport(Exception):
    """The connection failed; the outcome of the request is unknown."""


class RawConn:
    """One framed connection. Frames are built here so the exact bytes of
    any request can be captured and resent."""

    def __init__(self, path: str, timeout: float = 10.0, connect_wait: float = 5.0):
        deadline = time.monotonic() + connect_wait
        delay = 0.01
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                s.connect(path)
                break
            except OSError:
                s.close()
                if time.monotonic() > deadline:
                    raise Transport(f"cannot connect to {path}")
                time.sleep(delay)
                delay = min(delay * 2, 0.2)
        self.s = s
        self._serial = 0

    def frame(self, op: str, task_id: str, args: dict, rid: str | None = None) -> tuple[str, bytes]:
        self._serial += 1
        rid = rid or f"s{os.getpid()}-{threading.get_ident() % 100000}-{self._serial}"
        return rid, P.encode_frame({"v": P.VERSION, "id": rid, "op": op,
                                    "task_id": task_id, "args": args})

    def send_bytes(self, data: bytes) -> dict:
        try:
            self.s.sendall(data)
            body = P.read_frame(self.s)
        except (OSError, P.ProtocolError) as exc:
            raise Transport(str(exc))
        if body is None:
            raise Transport("closed by server")
        return json.loads(body.decode("utf-8"))

    def call(self, op: str, task_id: str, args: dict) -> dict:
        return self.send_bytes(self.frame(op, task_id, args)[1])

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


def classify(resp: dict) -> dict:
    """Reduce a response to an outcome record."""
    if resp.get("ok"):
        res = resp.get("result") or {}
        if isinstance(res, dict) and res.get("ok") is False:
            return {"ok": False, "denied": res.get("reason", "")}
        return {"ok": True, "receipt_id": (res or {}).get("receipt_id")}
    err = resp.get("error") or {}
    if err.get("code") == "refusal":
        return {"ok": False, "refusal": err.get("message", "")}
    return {"ok": False, "protocol": err.get("message", "")}


def normalize_reason(reason: str) -> str:
    """Group refusal reasons: grant ids differ per attempt."""
    return re.sub(r"g-\d+", "g-N", reason)


class StressSession:
    """Attacker state for one task: the socket path, the task id, the
    provenance it collected (a projection hash it was shown, the prompt
    hash `begin_task` returned) and its primary connection."""

    def __init__(self, socket_path: str, task_id: str = DEFAULT_TASK, begin: bool = True,
                 prompt_hash: str | None = None):
        self.path = socket_path
        self.task_id = task_id
        self.prompt_hash = prompt_hash
        self.projection_ref: str | None = None
        self.conn = RawConn(socket_path)
        self._seq = 0
        self._seq_lock = threading.Lock()
        if begin:
            resp = self.conn.call("begin_task", task_id, {})
            if not resp.get("ok"):
                raise RuntimeError(f"begin_task refused: {resp.get('error')}")
            self.prompt_hash = resp["result"]["prompt_hash"]
        self._collect_provenance()

    def _collect_provenance(self) -> None:
        g = self.conn.call("request_grant", self.task_id,
                           {"resource": RESOURCE, "operation": "read",
                            "projection": PROJECTION})
        gid = (g.get("result") or {}).get("grant_id")
        if not gid:
            raise RuntimeError(f"read grant not issued: {g}")
        r = self.conn.call("read", self.task_id, {"grant_id": gid, "resource": RESOURCE,
                                                   "projection": PROJECTION})
        if not r.get("ok"):
            raise RuntimeError(f"read refused: {r}")
        self.projection_ref = r["result"]["projection_meta"]["content_hash"]

    def derived_from(self) -> list:
        out = [{"type": "projection", "ref": self.projection_ref}]
        if self.prompt_hash:
            out.append({"type": "user_input", "ref": self.prompt_hash})
        return out

    def next_index(self) -> int:
        with self._seq_lock:
            i = self._seq
            self._seq += 1
            return i

    def effect_args(self, grant_id: str, i: int) -> dict:
        return {"grant_id": grant_id, "effect_name": EFFECT,
                "params": {"thread_id": THREAD, "body": f"stress-{i}"},
                "derived_from": self.derived_from()}

    def close(self) -> None:
        self.conn.close()

    # -- one attempt -------------------------------------------------------
    def grant(self, conn: RawConn) -> dict:
        try:
            resp = conn.call("request_grant", self.task_id,
                             {"resource": RESOURCE, "operation": EFFECT})
        except Transport as exc:
            return {"ok": False, "transport": str(exc)}
        out = classify(resp)
        if out["ok"]:
            out["grant_id"] = resp["result"].get("grant_id")
        return out

    def send(self, conn: RawConn, grant_id: str, i: int) -> dict:
        try:
            return classify(conn.call("effect", self.task_id, self.effect_args(grant_id, i)))
        except Transport as exc:
            return {"ok": False, "transport": str(exc)}

    def fresh_attempt(self, conn: RawConn, i: int) -> dict:
        g = self.grant(conn)
        rec = {"i": i, "grant_ok": g["ok"], "grant_id": g.get("grant_id")}
        if not g["ok"]:
            rec["effect"] = {"ok": False, **{k: v for k, v in g.items() if k != "ok"}}
            return rec
        rec["effect"] = self.send(conn, g["grant_id"], i)
        return rec


# -- attacks ---------------------------------------------------------------------

def _timed(fn):
    def wrapper(*a, **kw):
        t0 = time.monotonic()
        out = fn(*a, **kw)
        out["elapsed_s"] = round(time.monotonic() - t0, 4)
        return out
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_timed
def sequential_fresh_grants(sess: StressSession, n: int) -> dict:
    """A fresh send_message grant per attempt, one send on each."""
    return {"attack": "sequential",
            "outcomes": [sess.fresh_attempt(sess.conn, sess.next_index()) for _ in range(n)]}


@_timed
def repeat_same_grant(sess: StressSession, n: int) -> dict:
    """One grant, n sends on it."""
    g = sess.grant(sess.conn)
    outcomes = []
    for _ in range(n):
        i = sess.next_index()
        eff = (sess.send(sess.conn, g["grant_id"], i) if g["ok"]
               else {"ok": False, **{k: v for k, v in g.items() if k != "ok"}})
        outcomes.append({"i": i, "grant_ok": g["ok"], "grant_id": g.get("grant_id"),
                         "effect": eff})
    return {"attack": "same_grant", "outcomes": outcomes}


@_timed
def concurrent(sess: StressSession, n: int, workers: int) -> dict:
    """`workers` threads, each with its own connection, loop fresh grant ->
    send until n attempts in total. A barrier releases them together."""
    outcomes, lock = [], threading.Lock()
    remaining = [n]
    barrier = threading.Barrier(workers)
    errors = []

    def take() -> bool:
        with lock:
            if remaining[0] <= 0:
                return False
            remaining[0] -= 1
            return True

    def worker():
        try:
            conn = RawConn(sess.path)
        except Transport as exc:
            errors.append(str(exc))
            try:
                barrier.wait(5)
            except threading.BrokenBarrierError:
                pass
            return
        try:
            try:
                barrier.wait(5)
            except threading.BrokenBarrierError:
                pass
            while take():
                rec = sess.fresh_attempt(conn, sess.next_index())
                with lock:
                    outcomes.append(rec)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    outcomes.sort(key=lambda r: r["i"])
    return {"attack": "concurrent", "workers": workers, "connect_errors": errors,
            "outcomes": outcomes}


@_timed
def reconnect_between(sess: StressSession, n: int) -> dict:
    """Close and reopen the connection before every attempt."""
    outcomes = []
    for _ in range(n):
        sess.conn.close()
        sess.conn = RawConn(sess.path)
        outcomes.append(sess.fresh_attempt(sess.conn, sess.next_index()))
    return {"attack": "reconnect", "outcomes": outcomes}


@_timed
def second_process(sess: StressSession, n: int, timeout: float = 60.0) -> dict:
    """Untrusted-process restart: this process drops its connection and a
    second hostile process continues the same (still active) task without
    `begin_task`, carrying only what an attacker would persist (the socket
    path, the task id, the prompt hash). It collects its own projection
    provenance with a fresh read."""
    sess.conn.close()
    cmd = [sys.executable, os.path.join(ROOT, "run_effect_stress.py"),
           "--socket", sess.path, "--task", sess.task_id, "--attack", "sequential",
           "--n", str(n), "--as-process", "--no-begin"]
    if sess.prompt_hash:
        cmd += ["--prompt-hash", sess.prompt_hash]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": ROOT}
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=timeout)
    sess.conn = RawConn(sess.path)
    child = None
    for line in reversed(proc.stdout.splitlines()):
        try:
            child = json.loads(line)
            break
        except ValueError:
            continue
    out = {"attack": "second_process", "returncode": proc.returncode,
           "outcomes": (child or {}).get("outcomes", [])}
    if proc.returncode != 0 or child is None:
        out["stderr"] = proc.stderr[-2000:]
    return out


@_timed
def replay_effect_frame(sess: StressSession, n: int) -> dict:
    """Capture the exact bytes of one effect frame (a successful one if the
    budget allows) and resend them n times."""
    i = sess.next_index()
    g = sess.grant(sess.conn)
    if not g["ok"]:
        return {"attack": "replay", "captured_from_success": False, "outcomes": [
            {"i": i, "grant_ok": False, "effect": {"ok": False, **{k: v for k, v in g.items()
                                                                   if k != "ok"}}}]}
    _, frame = sess.conn.frame("effect", sess.task_id, sess.effect_args(g["grant_id"], i))
    try:
        first = classify(sess.conn.send_bytes(frame))
    except Transport as exc:
        first = {"ok": False, "transport": str(exc)}
    outcomes = [{"i": i, "grant_ok": True, "grant_id": g["grant_id"], "original": True,
                 "effect": first}]
    for _ in range(n):
        try:
            eff = classify(sess.conn.send_bytes(frame))
        except Transport as exc:
            eff = {"ok": False, "transport": str(exc)}
            sess.conn = RawConn(sess.path)
        outcomes.append({"i": i, "grant_ok": True, "grant_id": g["grant_id"],
                         "replay": True, "effect": eff})
    return {"attack": "replay", "captured_from_success": first["ok"],
            "frame_bytes": len(frame), "outcomes": outcomes}


FORGED_COUNTER_ARGS = [
    {"consumed": 0},
    {"budget": {"scope": "task", "consumed": 0, "cap": 1000000}},
    {"effects_used": 0},
    {"consumed": 0, "budget": {"scope": "task", "consumed": 0, "cap": 1000000},
     "effects_used": 0},
]


@_timed
def forged_counter(sess: StressSession) -> dict:
    """Effect frames carrying client-side consumption fields."""
    outcomes = []
    for extra in FORGED_COUNTER_ARGS:
        i = sess.next_index()
        g = sess.grant(sess.conn)
        args = sess.effect_args(g.get("grant_id") or "g-0", i)
        args.update(extra)
        try:
            eff = classify(sess.conn.call("effect", sess.task_id, args))
        except Transport as exc:
            eff = {"ok": False, "transport": str(exc)}
            sess.conn = RawConn(sess.path)
        outcomes.append({"i": i, "grant_ok": g["ok"], "grant_id": g.get("grant_id"),
                         "forged": sorted(extra), "effect": eff})
    return {"attack": "forged_counter", "outcomes": outcomes}


def forged_task_ids(task_id: str, closed: list[str] | None = None) -> list[str]:
    ids = list(closed or [])
    ids += ["t1-summarize-alice", "t3-note-followup", "t-never-defined",
            task_id.upper(), task_id + " ", task_id + "-2", "*", "-"]
    return [t for t in dict.fromkeys(ids) if t != task_id]


@_timed
def forged_task_id(sess: StressSession, closed: list[str] | None = None) -> dict:
    """Effects framed as another task (closed, designated-not-begun, never
    defined, near-miss spellings) on a live grant of the real task, plus an
    attempt to re-begin the active task (a relabelled restart)."""
    outcomes = []
    g = sess.grant(sess.conn)
    gid = g.get("grant_id") or "g-0"
    for other in forged_task_ids(sess.task_id, closed):
        i = sess.next_index()
        args = sess.effect_args(gid, i)
        try:
            eff = classify(sess.conn.call("effect", other, args))
        except Transport as exc:
            eff = {"ok": False, "transport": str(exc)}
            sess.conn = RawConn(sess.path)
        outcomes.append({"i": i, "grant_ok": g["ok"], "grant_id": gid, "framed_as": other,
                         "effect": eff})
    try:
        again = classify(sess.conn.call("begin_task", sess.task_id, {}))
    except Transport as exc:
        again = {"ok": False, "transport": str(exc)}
        sess.conn = RawConn(sess.path)
    outcomes.append({"i": None, "grant_ok": None, "op": "begin_task", "effect": again})
    return {"attack": "forged_task", "outcomes": outcomes}


@_timed
def forged_grant_id(sess: StressSession) -> dict:
    """Effects on grant ids the server never minted for this task."""
    forged = ["g-999", "g-0", "g--1", "t1-summarize-alice", "g-1/../g-2", "G-1", ""]
    outcomes = []
    for gid in forged:
        i = sess.next_index()
        try:
            eff = classify(sess.conn.call("effect", sess.task_id, sess.effect_args(gid, i)))
        except Transport as exc:
            eff = {"ok": False, "transport": str(exc)}
            sess.conn = RawConn(sess.path)
        outcomes.append({"i": i, "grant_ok": False, "grant_id": gid, "effect": eff})
    return {"attack": "forged_grant", "outcomes": outcomes}


@_timed
def reordered(sess: StressSession) -> dict:
    """Send the effect before its grant exists, predicting the next grant
    id from the last one seen; then request the grant; then the effect."""
    probe = sess.grant(sess.conn)       # learn the current grant sequence
    outcomes = [{"i": None, "step": "probe_grant", "grant_ok": probe["ok"],
                 "grant_id": probe.get("grant_id"), "effect": None}]
    m = re.fullmatch(r"g-(\d+)", str(probe.get("grant_id") or ""))
    predicted = f"g-{int(m.group(1)) + 1}" if m else "g-1"
    i = sess.next_index()
    early = sess.send(sess.conn, predicted, i)
    outcomes.append({"i": i, "step": "effect_before_grant", "grant_ok": False,
                     "grant_id": predicted, "effect": early})
    g = sess.grant(sess.conn)
    outcomes.append({"i": None, "step": "grant", "grant_ok": g["ok"],
                     "grant_id": g.get("grant_id"), "predicted_matched":
                     g.get("grant_id") == predicted, "effect": None})
    if g["ok"]:
        i = sess.next_index()
        outcomes.append({"i": i, "step": "effect_after_grant", "grant_ok": True,
                         "grant_id": g["grant_id"],
                         "effect": sess.send(sess.conn, g["grant_id"], i)})
    return {"attack": "reordered", "outcomes": outcomes}


# -- trusted-side observation (the ledger) ----------------------------------------

def ledger_rows(ledger_path: str) -> list[dict]:
    rows = []
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass        # a line being appended right now
    except FileNotFoundError:
        pass
    return rows


def effect_rows(ledger_path: str, task_id: str) -> list[dict]:
    return [r for r in ledger_rows(ledger_path)
            if r.get("kind") == "effect" and r.get("task_id") == task_id]


def count_landed(ledger_path: str, task_id: str) -> int:
    """Effects that landed, per the trusted ledger (never client outcomes)."""
    return len(effect_rows(ledger_path, task_id))


def budget_refusals(ledger_path: str, task_id: str) -> list[dict]:
    """Refusal receipts carrying a `budget` detail, for the task."""
    return [r for r in ledger_rows(ledger_path)
            if r.get("kind") == "refusal" and r.get("task_id") == task_id
            and "budget" in r]


def resource_policy_rows(ledger_path: str) -> list[dict]:
    return [r for r in ledger_rows(ledger_path) if r.get("kind") == "resource_policy"]


def summarize(result: dict) -> dict:
    """Client-observed counts for one attack result (no ledger)."""
    effects = [o["effect"] for o in result["outcomes"] if o.get("effect") is not None]
    refusals, protocol, denied, transport = Counter(), Counter(), Counter(), Counter()
    for e in effects:
        if "refusal" in e:
            refusals[normalize_reason(e["refusal"])] += 1
        elif "protocol" in e:
            protocol[e["protocol"]] += 1
        elif "denied" in e:
            denied[e["denied"]] += 1
        elif "transport" in e:
            transport[e["transport"]] += 1
    return {"attempts": len(effects),
            "client_successes": sum(1 for e in effects if e.get("ok")),
            "refusals": dict(refusals), "protocol_rejections": dict(protocol),
            "grant_denials": dict(denied), "transport_failures": dict(transport)}
