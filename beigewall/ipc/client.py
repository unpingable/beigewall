"""Untrusted-side client: `RemoteBroker` speaks `beigewall-ipc/1` and
presents the broker's method surface, so the unchanged ToolFacade and
Harness run against it. It knows the socket path and nothing else about
the trusted side. Imports nothing from the trusted modules except the
protocol constants and the RefusalError type's *shape* (re-declared here).
"""

import json
import socket
import time
import uuid

from . import protocol as P


class RefusalError(Exception):
    """Mirror of beigewall.broker.RefusalError for the facade."""

    def __init__(self, message: str, receipt_id: str, hint: dict | None = None):
        super().__init__(message)
        self.receipt_id = receipt_id
        self.hint = hint
        self.prior_effect: dict | None = None


class TransportError(Exception):
    """The request's outcome is unknown to the client."""


class IndeterminateError(Exception):
    """The trusted side says the effect MAY have occurred and could not be
    receipted (phase 6C). It must not be retried on a fresh grant."""

    def __init__(self, message: str, effect_id: str | None, grant_id: str | None):
        super().__init__(message)
        self.effect_id = effect_id
        self.grant_id = grant_id


class EvidenceError(Exception):
    """The trusted side could not record the intent: the effect did NOT
    occur (phase 6C); the trusted process is fail-stopped."""


class RemoteBroker:
    CONNECT_WAIT = 10.0

    def __init__(self, socket_path: str, timeout: float = 60.0):
        self._path = socket_path
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._telemetry = {"requests": 0, "latency_ms": [], "bytes_in": 0,
                           "bytes_out": 0, "reconnects": 0, "transport_failures": 0,
                           "effect_recoveries": 0,
                           "connects": 0}
        self._connect(initial=True)

    # -- transport ---------------------------------------------------------
    def _connect(self, initial: bool = False) -> None:
        deadline = time.monotonic() + self.CONNECT_WAIT
        delay = 0.05
        while True:
            try:
                if self._path.startswith("vsock:"):
                    # phase 7: "vsock:CID:PORT" (the host is CID 2)
                    _, cid, port = self._path.split(":")
                    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
                    s.settimeout(self._timeout)
                    s.connect((int(cid), int(port)))
                else:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(self._timeout)
                    s.connect(self._path)
                self._sock = s
                self._telemetry["connects"] += 1
                if not initial:
                    self._telemetry["reconnects"] += 1
                return
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if time.monotonic() > deadline:
                    raise TransportError("trusted process unreachable")
                time.sleep(delay)
                delay = min(delay * 2, 0.5)

    @staticmethod
    def _match_response(pending_id: str, response: dict) -> bool:
        """A response is accepted only if it echoes the id we are waiting
        for (stale-response defence)."""
        return isinstance(response, dict) and response.get("id") == pending_id

    def _call(self, op: str, task_id: str, args: dict):
        rid = uuid.uuid4().hex[:16]
        frame = P.encode_frame({"v": P.VERSION, "id": rid, "op": op,
                                "task_id": task_id, "args": args})
        if self._sock is None:
            self._connect()
        start = time.perf_counter()
        try:
            self._sock.sendall(frame)
            self._telemetry["bytes_out"] += len(frame)
            while True:
                body = P.read_frame(self._sock)
                if body is None:
                    raise OSError("connection closed by trusted process")
                self._telemetry["bytes_in"] += len(body) + 4
                resp = json.loads(body.decode("utf-8"))
                if self._match_response(rid, resp):
                    break
                # protocol-level rejections carry an empty id; everything
                # else that does not match is stale and discarded
                if resp.get("id") == "" and not resp.get("ok", True):
                    break
        except (OSError, P.ProtocolError, ValueError) as exc:
            self._telemetry["transport_failures"] += 1
            self._drop()
            raise TransportError(f"transport failure: result unknown ({exc})")
        self._telemetry["requests"] += 1
        self._telemetry["latency_ms"].append((time.perf_counter() - start) * 1000)
        if resp.get("ok"):
            return resp["result"]
        self._raise_for(resp)

    @staticmethod
    def _raise_for(resp: dict):
        """Turn an error response into the matching exception."""
        err = resp.get("error", {})
        if err.get("code") == "refusal":
            exc = RefusalError(err.get("message", "refused"), err.get("receipt_id", ""),
                               err.get("capability_hint"))
            exc.prior_effect = err.get("prior_effect")
            raise exc
        if err.get("code") == "indeterminate":
            raise IndeterminateError(err.get("message", "indeterminate"),
                                     err.get("effect_id"), err.get("grant_id"))
        if err.get("code") == "evidence":
            raise EvidenceError(err.get("message", "evidence unavailable"))
        raise P.ProtocolError(err.get("message", "protocol error"))

    def _drop(self) -> None:
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None

    def close(self) -> None:
        self._drop()

    def telemetry(self) -> dict:
        return {k: (list(v) if isinstance(v, list) else v)
                for k, v in self._telemetry.items()}

    # -- broker surface ----------------------------------------------------
    def hello(self) -> dict:
        return self._call("hello", "-", {})

    def begin_task(self, task_id: str) -> dict:
        return self._call("begin_task", task_id, {})

    def request_grant(self, task_id, resource, operation, projection=None, ttl=None,
                      budget=None) -> dict:
        args = {"resource": resource, "operation": operation}
        if projection is not None:
            args["projection"] = projection
        if ttl is not None:
            args["ttl"] = ttl
        if budget is not None:
            args["budget"] = {"reads": getattr(budget, "reads", None) or budget["reads"],
                              "objects": getattr(budget, "objects", None) or budget["objects"]}
        return self._call("request_grant", task_id, args)

    def read(self, task_id, grant_id, resource, projection) -> dict:
        return self._call("read", task_id, {"grant_id": grant_id, "resource": resource,
                                            "projection": projection})

    def effect(self, task_id, grant_id, effect_name, params, derived_from) -> dict:
        args = {"grant_id": grant_id, "effect_name": effect_name,
                "params": params, "derived_from": derived_from}
        try:
            return self._call("effect", task_id, args)
        except TransportError as first:
            # The result is unknown. Re-sending the SAME effect on the SAME
            # grant is safe: an effect grant is consumed by its first
            # success, so the trusted side either performs it now (the
            # first never arrived) or refuses "effect grant consumed" and
            # names the prior effect's outcome. A fresh grant would risk a
            # duplicate (phase 5C observed one). One recovery attempt only.
            self._telemetry["effect_recoveries"] += 1
            try:
                return self._call("effect", task_id, args)
            except RefusalError as exc:
                if exc.prior_effect is not None:
                    prior = exc.prior_effect
                    if prior.get("status") == "committed":
                        return {"ok": True, "receipt_id": prior.get("receipt_id"),
                                "effect_id": prior.get("effect_id"), "recovered": True,
                                "summary": None}
                    raise IndeterminateError("effect indeterminate after recovery",
                                             prior.get("effect_id"), grant_id)
                raise
            except TransportError as second:
                raise TransportError(f"{first}; recovery attempt also failed: {second}")

    def discover(self, task_id, resource=None) -> dict:
        return self._call("discover", task_id, {} if resource is None else {"resource": resource})

    def report_unmediated(self, task_id, attempted, args_repr="") -> dict:
        return self._call("report_unmediated", task_id,
                          {"attempted": attempted[:64], "args_repr": args_repr[:200]})

    def narrative(self, task_id, claims, declared_effects, summary) -> dict:
        return self._call("narrative", task_id, {"claims": claims,
                                                 "declared_effects": declared_effects,
                                                 "summary": summary})

    def task_end(self, task_id, answer, truncated) -> dict:
        return self._call("task_end", task_id, {"answer": answer, "truncated": truncated})
