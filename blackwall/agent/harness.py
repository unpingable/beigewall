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
    def __init__(self, broker: CapabilityBroker, ledger: Ledger, task_id: str):
        self._broker = broker
        self._ledger = ledger
        self._task_id = task_id

    def request_grant(self, resource: str, operation: str,
                      projection: str | None = None, ttl: float | None = None,
                      budget: dict | None = None) -> dict:
        kwargs: dict = {}
        if ttl is not None:
            kwargs["ttl"] = ttl
        if budget is not None:
            kwargs["budget"] = Budget(reads=budget["reads"],
                                      objects=budget["objects"])
        return self._broker.request_grant(self._task_id, resource, operation,
                                          projection=projection, **kwargs)

    def read(self, grant_id: str, resource: str, projection: str) -> dict:
        try:
            return self._broker.read(self._task_id, grant_id, resource,
                                     projection=projection)
        except RefusalError as exc:
            return {"ok": False, "error": str(exc), "receipt_id": exc.receipt_id}

    def effect(self, grant_id: str, effect_name: str, params: dict,
               derived_from: list[dict]) -> dict:
        try:
            return self._broker.effect(self._task_id, grant_id, effect_name,
                                       params, derived_from)
        except RefusalError as exc:
            return {"ok": False, "error": str(exc), "receipt_id": exc.receipt_id}

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
    def __init__(self, broker: CapabilityBroker, ledger: Ledger):
        self._broker = broker
        self._ledger = ledger

    def run_task(self, task_id: str, driver, prompt: str,
                 max_steps: int = 30) -> RunResult:
        prompt_hash = canonical_hash({"prompt": prompt})
        self._broker.begin_task(task_id, prompt_hash)
        facade = ToolFacade(self._broker, self._ledger, task_id)
        driver.begin({"task_id": task_id, "prompt": prompt,
                      "prompt_hash": prompt_hash})
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
        if op == "unmediated":
            target = getattr(facade, move["call"])  # always the trap
            return target(*move.get("args", []))
        if op == "skip":
            return {"ok": False, "skipped": move.get("reason", "scripted skip")}
        return {"ok": False, "error": f"unknown move op {op!r}"}
