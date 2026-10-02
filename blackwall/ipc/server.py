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
import fcntl
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
from ..resources import ResourceConfigError, ResourceLedger, ResourcePolicy
from ..tasks import LogicalClock, experiment_policy, task_suite
from . import protocol as P

READ_TIMEOUT = 30.0


def task_id_of(req: dict) -> str:
    return req.get("task_id", "") if isinstance(req, dict) else ""


class TrustedServer:
    def __init__(self, socket_path: str, ledger_path: str, condition: str = "control",
                 run_id: str | None = None, tasks: list[str] | None = None,
                 resource_policy: ResourcePolicy | None = None):
        if condition not in ("control", "global", "denial", "task"):
            raise ValueError(f"unknown condition {condition!r}")
        self.socket_path = socket_path
        self.ledger_path = ledger_path
        # exclusive ownership of the ledger: a second server on the same
        # ledger (or socket) must not start (identity corruption otherwise)
        self._lock_fh = open(ledger_path + ".lock", "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"ledger {ledger_path} is owned by another server")
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
        self.resource_policy = resource_policy or ResourcePolicy()
        self.resources = None
        prior_policy = None
        for r in prior:
            if r.get("kind") == "resource_policy":
                prior_policy = r
        if prior_policy is not None:
            # a restart may keep or tighten the bounds of the run it resumes;
            # loosening or dropping them silently is a reset by another
            # name (hostile review F2)
            looser = self.resource_policy.looser_than(prior_policy)
            if looser:
                raise SystemExit("error: resource policy looser than the prior run's on "
                                 f"{looser}; refusing to start")
        if self.resource_policy.bounded:
            from ..broker import effect_resource
            self.resources = ResourceLedger(self.resource_policy)
            if prior:
                # consumption never resets on restart: rebuilt from receipts
                self.resources.rebuild(prior, effect_resource)
        self.broker = CapabilityBroker(self.corpus, self.authority, self.ledger,
                                       clock=self.clock.now, discovery_mode=self.mode,
                                       resources=self.resources)
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
        # The server owns task identity: the client may begin only tasks the
        # orchestrator designated for this run, each at most once, one at a
        # time; ops are accepted only for the active task; a closed task is
        # never reopened, and closure survives a restart (derived from the
        # ledger). Without this a client could relabel itself as any task
        # (hostile review). Order is not enforced: it adds no authority.
        self.designated = set(tasks) if tasks else set(self.tasks)
        for tid in self.designated:
            if tid not in self.tasks:
                raise ValueError(f"unknown task {tid!r}")
        self.active: str | None = None
        if self.resource_policy.bounded:
            self.ledger.record("resource_policy", "", **self.resource_policy.to_dict(),
                               rebuilt_consumed=(self.resources.snapshot() if prior else None),
                               rebuilt_over_cap=(self.resources.over_cap() if prior else None))
        # only rows the server attributed to a task close it; rejected
        # frames carry no task attribution (a client could otherwise lock a
        # task out by claiming its id in a bad frame)
        self.closed: set[str] = {r["task_id"] for r in prior
                                 if r.get("task_id") in self.tasks
                                 and not r.get("kind", "").startswith("ipc_")}
        self._lock = threading.Lock()   # the broker is single-threaded by design
        try:
            os.chmod(ledger_path, 0o600)
        except OSError:
            pass
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
                raise P.ProtocolError("unknown_task")
            if self.active is not None:
                raise P.ProtocolError("task_already_active")
            if task_id not in self.designated:
                raise P.ProtocolError("task_not_designated")
            if task_id in self.closed:
                raise P.ProtocolError("task_closed")
            prompt_hash = canonical_hash({"prompt": task.prompt})
            self.broker.begin_task(task_id, prompt_hash)
            self.active = task_id
            result = {"prompt": task.prompt, "prompt_hash": prompt_hash}
            if self.mode == "task":
                result["capabilities"] = self.broker.task_view(task_id, task.prompt)["view"]
            return P.ok_response(rid, result)
        if task_id != self.active:
            raise P.ProtocolError("task_closed" if task_id in self.closed else "task_not_active")
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
                                       narrative_hash=canonical_hash(narrative),
                                       self_reported=True)
                return P.ok_response(rid, {"receipt_id": r.receipt_id})
            if op == "task_end":
                r = self.ledger.record("task_end", task_id, answer=args["answer"],
                                       answer_hash=canonical_hash({"answer": args["answer"]}),
                                       truncated=args["truncated"], self_reported=True)
                self.closed.add(task_id)
                self.active = None
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
                    self._try_send(conn, P.error_response(exc.req_id, "protocol", exc.reason))
                    if exc.close:
                        return
                    continue
                try:
                    with self._lock:
                        resp = self.handle(req)
                        try:
                            P.encode_frame(resp)
                        except P.ProtocolError:
                            # the broker already receipted the operation; the
                            # client must learn the result did not reach it
                            rr = resp.get("result") or {}
                            self.ledger.record("ipc_reject", "",
                                               reason="response_too_large", conn=serial,
                                               op=req.get("op"), claimed_task=task_id_of(req),
                                               receipt_id=rr.get("receipt_id"))
                            resp = P.error_response(req["id"], "protocol",
                                                    "response_too_large")
                except P.ProtocolError as exc:
                    with self._lock:
                        self.ledger.record("ipc_reject", "", reason=exc.reason, conn=serial,
                                           op=req.get("op"), claimed_task=task_id_of(req))
                    resp = P.error_response(req["id"], "protocol", exc.reason)
                except Exception as exc:   # fail closed, never crash the membrane
                    with self._lock:
                        self.ledger.record("ipc_reject", "",
                                           reason=f"internal:{type(exc).__name__}",
                                           conn=serial, op=req.get("op"),
                                           claimed_task=task_id_of(req))
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
            # never take over a live socket: probe it first
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.5)
                probe.connect(self.socket_path)
                probe.close()
                raise SystemExit(f"socket {self.socket_path} is live")
            except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError) as exc:
                if isinstance(exc, SystemExit):
                    raise
            os.unlink(self.socket_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self._socket_ino = os.stat(self.socket_path).st_ino
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
                # unlink only the socket this server created
                if os.stat(self.socket_path).st_ino == self._socket_ino:
                    os.unlink(self.socket_path)
            except OSError:
                pass
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
            except OSError:
                pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", required=True)
    p.add_argument("--ledger", required=True)
    p.add_argument("--condition", default="control",
                   choices=("control", "global", "denial", "task"))
    p.add_argument("--run-id", default=None)
    p.add_argument("--tasks", default=None,
                   help="comma-separated tasks designated for this run "
                        "(default: the full suite)")
    p.add_argument("--effect-count", type=int, default=None,
                   help="max successful effects per (task, effect, target resource)")
    p.add_argument("--task-effects", type=int, default=None,
                   help="max successful effects per task")
    p.add_argument("--rate", default=None, help="n/seconds per (task, effect)")
    a = p.parse_args(argv)
    tasks = [t for t in a.tasks.split(",") if t] if a.tasks else None
    try:
        policy = ResourcePolicy.parse(a.effect_count, a.task_effects, a.rate)
    except ResourceConfigError as exc:
        print(f"error: invalid resource policy: {exc}", file=sys.stderr)
        return 2
    TrustedServer(a.socket, a.ledger, a.condition, a.run_id, tasks, policy).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
