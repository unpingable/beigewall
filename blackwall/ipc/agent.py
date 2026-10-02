"""Untrusted-side harness for the process-separated architecture.

Re-implements the agent tool loop over `RemoteBroker` without importing any
trusted module (broker, corpus, authority, projections, effects, receipts):
the facade exposes request_grant / read / effect (and discover in the
`global` condition), traps every other attribute as an unmediated attempt
and *reports* it over IPC (advisory), and the harness records narrative and
final answer through the protocol. Prompts and prompt hashes come from the
trusted side via `begin_task`; the agent process cannot choose them.
"""

from .client import RefusalError, RemoteBroker, TransportError


class RemoteFacade:
    def __init__(self, broker: RemoteBroker, task_id: str, discovery: bool = False):
        self._broker = broker
        self._task_id = task_id
        if discovery:
            self.discover = self._discover

    def _discover(self, resource=None):
        try:
            return self._broker.discover(self._task_id, resource)
        except RefusalError as exc:
            return self._refused(exc)

    def request_grant(self, resource, operation, projection=None, ttl=None, budget=None):
        if not isinstance(resource, str) or not isinstance(operation, str):
            return {"ok": False, "malformed": True, "error": "malformed request arguments"}
        return self._broker.request_grant(self._task_id, resource, operation,
                                          projection=projection, ttl=ttl, budget=budget)

    def read(self, grant_id, resource, projection):
        try:
            return self._broker.read(self._task_id, grant_id, resource, projection)
        except RefusalError as exc:
            return self._refused(exc)

    def effect(self, grant_id, effect_name, params, derived_from):
        try:
            return self._broker.effect(self._task_id, grant_id, effect_name, params,
                                       derived_from)
        except RefusalError as exc:
            return self._refused(exc)

    @staticmethod
    def _refused(exc: RefusalError) -> dict:
        out = {"ok": False, "error": str(exc), "receipt_id": exc.receipt_id}
        if exc.hint is not None:
            out["capability_hint"] = exc.hint
        return out

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def trap(*args, **kwargs):
            try:
                self._broker.report_unmediated(self._task_id, name, repr(args)[:200])
            except (RefusalError, TransportError, Exception):
                pass
            return {"ok": False, "unmediated_refused": True,
                    "error": f"no mediated capability named {name!r}; "
                             "only request_grant/read/effect exist"}
        return trap


class RemoteHarness:
    def __init__(self, broker: RemoteBroker, discovery_mode: str | None = None):
        self._broker = broker
        self._mode = discovery_mode

    def run_task(self, task_id: str, driver, max_steps: int = 40) -> dict:
        begun = self._broker.begin_task(task_id)
        facade = RemoteFacade(self._broker, task_id, discovery=self._mode == "global")
        task_view = {"task_id": task_id, "prompt": begun["prompt"],
                     "prompt_hash": begun["prompt_hash"]}
        driver.begin(task_view)
        observations, answer, truncated = [], "", True
        transport_failures = 0
        for _ in range(max_steps):
            move = driver.act(list(observations))
            if move.get("op") == "final":
                answer, truncated = move.get("answer", "") or "", False
                break
            try:
                observations.append(self._execute(facade, move))
            except TransportError as exc:
                transport_failures += 1
                observations.append({"ok": False, "transport_failure": True,
                                     "error": str(exc)})
        narrative = driver.narrate()
        try:
            self._broker.narrative(task_id, narrative.get("claims", []),
                                   narrative.get("declared_effects", []),
                                   narrative.get("summary", ""))
            self._broker.task_end(task_id, answer, truncated)
        except TransportError:
            transport_failures += 1
        return {"task_id": task_id, "answer": answer, "truncated": truncated,
                "transport_failures": transport_failures}

    @staticmethod
    def _execute(facade: RemoteFacade, move: dict) -> dict:
        op = move.get("op")
        if op == "request_grant":
            return facade.request_grant(move["resource"], move["operation"],
                                        projection=move.get("projection"),
                                        ttl=move.get("ttl"), budget=move.get("budget"))
        if op == "read":
            return facade.read(move["grant_id"], move["resource"], move["projection"])
        if op == "effect":
            return facade.effect(move["grant_id"], move["effect"],
                                 move.get("params", {}), move.get("derived_from", []))
        if op == "discover":
            return getattr(facade, "discover")(move.get("resource"))
        if op == "unmediated":
            return getattr(facade, move["call"])(*move.get("args", []))
        if op == "skip":
            return {"ok": False, "skipped": move.get("reason", "scripted skip")}
        if op == "malformed":
            return {"ok": False, "malformed": True,
                    "error": f"malformed broker request: {move.get('detail', 'unparseable')}"}
        return {"ok": False, "error": f"unknown move op {op!r}"}
