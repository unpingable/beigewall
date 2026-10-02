"""Trusted membrane process (phase 6).

Owns the corpus, authority, broker, projections, effects, catalog and
ledger. Serves `blackwall-ipc/1` on a Unix-domain socket; every byte from
the socket is hostile. It never evaluates, imports or opens anything named
by a client; the only filesystem paths it knows come from its own argv.
Task definitions and prompts are the server's: the client learns a task's
prompt and prompt hash from `begin_task` and cannot choose either.

Receipts: the broker's own receipts (unchanged) plus `ipc_connection`
(connect/disconnect), `ipc_reject` (protocol-level rejections) and
`trusted_restart` (server started over an existing ledger; sequence and
clock continue, authority state does not).
"""

import argparse
import os
import re
import signal
import socket
import sys
import threading
import time
import uuid

from ..authority import PolicyAuthority
from ..broker import CapabilityBroker, RefusalError
from ..corpus import build_demo_corpus
from ..receipts import Ledger, canonical_hash
from ..tasks import LogicalClock, experiment_policy, task_suite
from . import protocol as P

READ_TIMEOUT = 30.0


class TrustedServer:
    def __init__(self, socket_path: str, ledger_path: str, condition: str = "control",
                 run_id: str | None = None):
        if condition not in ("control", "global", "denial", "task"):
            raise ValueError(f"unknown condition {condition!r}")
        self.socket_path = socket_path
        self.ledger_path = ledger_path
        self.condition = condition
        self.mode = {"control": None, "global": "global", "denial": "denial",
                     "task": "task"}[condition]
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.clock = LogicalClock()
        prior = Ledger.existing_rows(ledger_path)
        self.ledger = Ledger(clock=self.clock.now, sink_path=ledger_path)
        self.tasks = {t.task_id: t for t in task_suite()}
        if prior:
            self.ledger.resume_after(prior)
            self.clock.t = max((r["ts"] for r in prior), default=1000.0) + 1.0
        self.corpus = build_demo_corpus()
        self.authority = PolicyAuthority(experiment_policy(), clock=self.clock.now)
        self.broker = CapabilityBroker(self.corpus, self.authority, self.ledger,
                                       clock=self.clock.now, discovery_mode=self.mode)
        if prior:
            # grant identities must not collide across a restart either:
            # continue the authority's counter after the highest minted id
            # (authority *state* — the grants themselves — is not restored)
            highest = 0
            for r in prior:
                if r.get("kind") == "grant_mint":
                    m = re.fullmatch(r"g-(\d+)", str(r.get("grant_id", "")))
                    if m:
                        highest = max(highest, int(m.group(1)))
            self.authority._grant_seq = highest
            self.ledger.record("trusted_restart", "", run_id=self.run_id,
                               prior_receipts=len(prior), grant_seq_resumed=highest)
        self.begun: set[str] = set()
        self._lock = threading.Lock()   # the broker is single-threaded by design
        self.started_at = time.time()
        self._conn_serial = 0
        self._stop = False

    # -- dispatch ---------------------------------------------------------
    def handle(self, req: dict) -> dict:
        op, task_id, args, rid = req["op"], req["task_id"], req["args"], req["id"]
        if op == "hello":
            return P.ok_response(rid, {"protocol": P.PROTOCOL, "server_run_id": self.run_id,
                                       "condition": self.condition,
                                       "started_at": self.started_at})
        if op == "begin_task":
            task = self.tasks.get(task_id)
            if task is None:
                return P.error_response(rid, "protocol", "unknown task")
            prompt_hash = canonical_hash({"prompt": task.prompt})
            self.broker.begin_task(task_id, prompt_hash)
            self.begun.add(task_id)
            return P.ok_response(rid, {"prompt": task.prompt, "prompt_hash": prompt_hash})
        if task_id not in self.begun:
            return P.error_response(rid, "protocol", "task not begun")
        try:
            if op == "request_grant":
                kwargs = {}
                if "ttl" in args:
                    kwargs["ttl"] = args["ttl"]
                if "budget" in args:
                    from ..grants import Budget
                    kwargs["budget"] = Budget(reads=args["budget"]["reads"],
                                              objects=args["budget"]["objects"])
                return P.ok_response(rid, self.broker.request_grant(
                    task_id, args["resource"], args["operation"],
                    projection=args.get("projection"), **kwargs))
            if op == "read":
                return P.ok_response(rid, self.broker.read(
                    task_id, args["grant_id"], args["resource"], args["projection"]))
            if op == "effect":
                return P.ok_response(rid, self.broker.effect(
                    task_id, args["grant_id"], args["effect_name"], args["params"],
                    args["derived_from"]))
            if op == "discover":
                if self.mode != "global":
                    raise P.ProtocolError("unknown_op")
                return P.ok_response(rid, self.broker.discover(task_id, args.get("resource")))
            if op == "report_unmediated":
                r = self.ledger.record("unmediated_attempt", task_id,
                                       attempted=args["attempted"],
                                       args_repr=args.get("args_repr", ""),
                                       self_reported=True)
                return P.ok_response(rid, {"receipt_id": r.receipt_id})
            if op == "narrative":
                narrative = {"claims": args["claims"],
                             "declared_effects": args["declared_effects"],
                             "summary": args["summary"]}
                r = self.ledger.record("narrative", task_id, **narrative,
                                       narrative_hash=canonical_hash(narrative))
                return P.ok_response(rid, {"receipt_id": r.receipt_id})
            if op == "task_end":
                r = self.ledger.record("task_end", task_id, answer=args["answer"],
                                       answer_hash=canonical_hash({"answer": args["answer"]}),
                                       truncated=args["truncated"])
                return P.ok_response(rid, {"receipt_id": r.receipt_id})
        except RefusalError as exc:
            err = P.error_response(rid, "refusal", str(exc), exc.receipt_id)
            if exc.hint is not None:
                err["error"]["capability_hint"] = exc.hint
            return err
        raise P.ProtocolError("unknown_op")

    # -- connections ------------------------------------------------------
    def serve_connection(self, conn: socket.socket) -> None:
        with self._lock:
            self._conn_serial += 1
            serial = self._conn_serial
            self.ledger.record("ipc_connection", "", event="connect", conn=serial)
        conn.settimeout(READ_TIMEOUT)
        try:
            while not self._stop:
                try:
                    body = P.read_frame(conn)
                except socket.timeout:
                    with self._lock:
                        self.ledger.record("ipc_reject", "", reason="timeout", conn=serial)
                    return
                except P.ProtocolError as exc:
                    with self._lock:
                        self.ledger.record("ipc_reject", "", reason=exc.reason, conn=serial)
                    self._try_send(conn, P.error_response(None, "protocol", exc.reason))
                    return
                if body is None:
                    return
                try:
                    req = P.decode(body)
                except P.ProtocolError as exc:
                    with self._lock:
                        self.ledger.record("ipc_reject", "", reason=exc.reason, conn=serial,
                                           bytes=len(body))
                    self._try_send(conn, P.error_response(None, "protocol", exc.reason))
                    if exc.close:
                        return
                    continue
                try:
                    with self._lock:
                        resp = self.handle(req)
                except P.ProtocolError as exc:
                    with self._lock:
                        self.ledger.record("ipc_reject", req.get("task_id", ""),
                                           reason=exc.reason, conn=serial, op=req.get("op"))
                    resp = P.error_response(req["id"], "protocol", exc.reason)
                except Exception as exc:   # fail closed, never crash the membrane
                    with self._lock:
                        self.ledger.record("ipc_reject", req.get("task_id", ""),
                                           reason=f"internal:{type(exc).__name__}",
                                           conn=serial, op=req.get("op"))
                    resp = P.error_response(req["id"], "protocol", "internal error")
                if not self._try_send(conn, resp):
                    return
        finally:
            with self._lock:
                self.ledger.record("ipc_connection", "", event="disconnect", conn=serial)
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _try_send(conn: socket.socket, resp: dict) -> bool:
        try:
            conn.sendall(P.encode_frame(resp))
            return True
        except (OSError, P.ProtocolError):
            return False

    def serve_forever(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        srv.listen(8)
        srv.settimeout(0.5)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        try:
            while not self._stop:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                # one thread per connection; dispatch is serialized by the lock
                threading.Thread(target=self.serve_connection, args=(conn,),
                                 daemon=True).start()
        finally:
            srv.close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", required=True)
    p.add_argument("--ledger", required=True)
    p.add_argument("--condition", default="control",
                   choices=("control", "global", "denial", "task"))
    p.add_argument("--run-id", default=None)
    a = p.parse_args(argv)
    TrustedServer(a.socket, a.ledger, a.condition, a.run_id).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
