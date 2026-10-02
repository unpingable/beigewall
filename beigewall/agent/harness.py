"""Agent harness: a simple tool loop around the broker's sole tool surface.

The driver never sees the corpus, the authority, or the ledger. It receives a
TaskView (prompt + hash) and a ToolFacade exposing exactly request_grant, read,
and effect. Any other attribute access on the facade is an unmediated
capability attempt: trapped, receipted, and refused.
"""

from ..broker import CapabilityBroker, RefusalError
from ..grants import Budget
from ..receipts import Ledger, canonical_hash


class ToolFacade:
    def __init__(self, broker: CapabilityBroker, ledger: Ledger, task_id: str,
                 discovery: bool = False):
        self._broker = broker
        self._ledger = ledger
        self._task_id = task_id
        if discovery:   # global discovery only; other modes have no tool
            # The attribute exists only in the treatment condition; without it
            # a call to `discover` is an unmediated attempt like any other.
            self.discover = self._discover

    def _discover(self, resource: str | None = None) -> dict:
        return self._broker.discover(self._task_id, resource=resource)

    def request_grant(self, resource: str, operation: str,
                      projection: str | None = None, ttl: float | None = None,
                      budget: dict | None = None) -> dict:
        import math
        bad = (not isinstance(resource, str) or not isinstance(operation, str)
               or (projection is not None and not isinstance(projection, str))
               or (ttl is not None and isinstance(ttl, bool))
               or (ttl is not None and not isinstance(ttl, (int, float)))
               or (ttl is not None and isinstance(ttl, (int, float))
                   and (not math.isfinite(ttl) or ttl <= 0))
               or (budget is not None and not (
                   isinstance(budget, dict)
                   and isinstance(budget.get("reads"), int)
                   and isinstance(budget.get("objects"), int))))
        if bad:
            # a malformed request is receipted and refused, never a crash
            # (a crash would discard the run and bias resumed samples)
            r = self._ledger.record("refusal", self._task_id,
                                    operation="request_grant", grant_id="",
                                    resource=repr(resource)[:80],
                                    reason="malformed request arguments")
            return {"ok": False, "malformed": True, "receipt_id": r.receipt_id,
                    "error": "malformed request arguments"}
        kwargs: dict = {}
        if ttl is not None:
            kwargs["ttl"] = ttl
        if budget is not None:
            kwargs["budget"] = Budget(reads=budget["reads"],
                                      objects=budget["objects"])
        return self._broker.request_grant(self._task_id, resource, operation,
                                          projection=projection, **kwargs)

    def read(self, grant_id: str, resource: str, projection: str) -> dict:
        if not all(isinstance(x, str) for x in (grant_id, resource, projection)):
            r = self._ledger.record("refusal", self._task_id, operation="read",
                                    grant_id=repr(grant_id)[:40],
                                    resource=repr(resource)[:80],
                                    reason="malformed request arguments")
            return {"ok": False, "malformed": True, "receipt_id": r.receipt_id,
                    "error": "malformed request arguments"}
        try:
            return self._broker.read(self._task_id, grant_id, resource,
                                     projection=projection)
        except RefusalError as exc:
            return self._refused(exc)

    @staticmethod
    def _refused(exc: RefusalError) -> dict:
        out = {"ok": False, "error": str(exc), "receipt_id": exc.receipt_id}
        if exc.hint is not None:
            out["capability_hint"] = exc.hint
        return out

    def effect(self, grant_id: str, effect_name: str, params: dict,
               derived_from: list[dict]) -> dict:
        if (not isinstance(grant_id, str) or not isinstance(effect_name, str)
                or not isinstance(params, dict)
                or not isinstance(derived_from, list)):
            r = self._ledger.record("refusal", self._task_id,
                                    operation=str(effect_name)[:40],
                                    grant_id=repr(grant_id)[:40], resource="",
                                    reason="malformed request arguments")
            return {"ok": False, "malformed": True, "receipt_id": r.receipt_id,
                    "error": "malformed request arguments"}
        try:
            return self._broker.effect(self._task_id, grant_id, effect_name,
                                       params, derived_from)
        except RefusalError as exc:
            return self._refused(exc)

    def __getattr__(self, name: str):
        # Only missing attributes land here: request_grant/read/effect exist.
        if name.startswith("_"):
            raise AttributeError(name)

        def trap(*args, **kwargs):
            self._ledger.record("unmediated_attempt", self._task_id,
                                attempted=name,
                                args_repr=repr(args)[:200])
            return {"ok": False, "unmediated_refused": True,
                    "error": f"no mediated capability named {name!r}; "
                             "only request_grant/read/effect exist"}

        return trap


class RunResult:
    def __init__(self, task_id: str, answer: str, observations: list[dict],
                 truncated: bool):
        self.task_id = task_id
        self.answer = answer
        self.observations = observations
        self.truncated = truncated


class Harness:
    def __init__(self, broker: CapabilityBroker, ledger: Ledger,
                 discovery: bool = False, discovery_mode: str | None = None):
        self._broker = broker
        self._ledger = ledger
        self._mode = discovery_mode or ("global" if discovery else None)
        self._discovery = self._mode == "global"

    def run_task(self, task_id: str, driver, prompt: str,
                 max_steps: int = 30) -> RunResult:
        prompt_hash = canonical_hash({"prompt": prompt})
        self._broker.begin_task(task_id, prompt_hash)
        facade = ToolFacade(self._broker, self._ledger, task_id,
                            discovery=self._discovery)
        task_view = {"task_id": task_id, "prompt": prompt,
                     "prompt_hash": prompt_hash}
        if self._mode == "task":
            task_view["capabilities"] = self._broker.task_view(
                task_id, prompt)["view"]
        driver.begin(task_view)
        observations: list[dict] = []
        answer = None
        truncated = True
        for _ in range(max_steps):
            move = driver.act(list(observations))
            if move.get("op") == "final":
                answer = move.get("answer", "")
                truncated = False
                break
            observations.append(self._execute(facade, move))
        else:
            answer = ""

        narrative = driver.narrate()
        self._ledger.record("narrative", task_id,
                            claims=narrative.get("claims", []),
                            declared_effects=narrative.get("declared_effects", []),
                            summary=narrative.get("summary", ""),
                            narrative_hash=canonical_hash(narrative))
        self._ledger.record("task_end", task_id, answer=answer,
                            answer_hash=canonical_hash({"answer": answer}),
                            truncated=truncated)
        return RunResult(task_id, answer, observations, truncated)

    @staticmethod
    def _execute(facade: ToolFacade, move: dict) -> dict:
        op = move.get("op")
        if op == "request_grant":
            return facade.request_grant(move["resource"], move["operation"],
                                        projection=move.get("projection"),
                                        ttl=move.get("ttl"),
                                        budget=move.get("budget"))
        if op == "read":
            return facade.read(move["grant_id"], move["resource"],
                               move["projection"])
        if op == "effect":
            return facade.effect(move["grant_id"], move["effect"],
                                 move.get("params", {}),
                                 move.get("derived_from", []))
        if op == "discover":
            # resolves to the trap when discovery is disabled
            return getattr(facade, "discover")(move.get("resource"))
        if op == "unmediated":
            target = getattr(facade, move["call"])  # always the trap
            return target(*move.get("args", []))
        if op == "skip":
            return {"ok": False, "skipped": move.get("reason", "scripted skip")}
        if op == "malformed":
            return {"ok": False, "malformed": True,
                    "error": f"malformed broker request: "
                             f"{move.get('detail', 'unparseable')}"}
        return {"ok": False, "error": f"unknown move op {op!r}"}
