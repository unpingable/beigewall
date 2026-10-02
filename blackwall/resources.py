"""Resource budgets for already-authorized effects (phase 6B).

Authorization (the policy authority) decides whether an effect may happen.
This module constrains how much of that authority may be consumed: a
cumulative count per (task, effect, target resource), a per-task total, and
a wall-clock rate per (task, effect). All state is trusted-side; nothing a
client supplies is read. The counters are explicit state with an invariant
checked after every update; they are rebuilt from effect receipts on a
trusted restart so consumption never silently resets.
"""

import math
import time
from dataclasses import dataclass


class ResourceConfigError(ValueError):
    """A budget configuration that is not a positive, finite bound."""


def _positive_int(value, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ResourceConfigError(f"{name} must be a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class ResourcePolicy:
    effect_count: int | None = None     # per (task, effect, resource)
    task_effects: int | None = None     # per task, all effects
    rate_n: int | None = None           # at most rate_n successful effects ...
    rate_seconds: float | None = None   # ... per rate_seconds of wall clock, per (task, effect)

    def __post_init__(self):
        _positive_int(self.effect_count, "effect_count")
        _positive_int(self.task_effects, "task_effects")
        _positive_int(self.rate_n, "rate_n")
        if (self.rate_n is None) != (self.rate_seconds is None):
            raise ResourceConfigError("rate needs both n and seconds")
        if self.rate_seconds is not None:
            s = self.rate_seconds
            if (isinstance(s, bool) or not isinstance(s, (int, float))
                    or not math.isfinite(s) or s <= 0):
                raise ResourceConfigError(f"rate seconds must be positive and finite, got {s!r}")

    @classmethod
    def parse(cls, effect_count=None, task_effects=None, rate: str | None = None):
        n = secs = None
        if rate:
            try:
                a, b = rate.split("/", 1)
                n, secs = int(a), float(b)
            except ValueError:
                raise ResourceConfigError(f"rate must look like 'n/seconds', got {rate!r}")
        return cls(effect_count=effect_count, task_effects=task_effects,
                   rate_n=n, rate_seconds=secs)

    @property
    def bounded(self) -> bool:
        return any(v is not None for v in (self.effect_count, self.task_effects, self.rate_n))

    def to_dict(self) -> dict:
        return {"effect_count": self.effect_count, "task_effects": self.task_effects,
                "rate": ({"n": self.rate_n, "seconds": self.rate_seconds}
                         if self.rate_n is not None else None)}


class ResourceLedger:
    """Trusted consumption state. `check` and `consume` are called inside
    the broker's effect path (under the server's dispatch lock); the
    invariant is asserted on the state after every consume."""

    def __init__(self, policy: ResourcePolicy, clock=None):
        self.policy = policy
        self._wall = clock or time.monotonic
        self.consumed: dict[tuple, int] = {}       # (task, effect, resource) -> n
        self.task_total: dict[str, int] = {}
        self.stamps: dict[tuple, list[float]] = {}  # (task, effect) -> wall times

    # -- queries -------------------------------------------------------
    def check(self, task_id: str, effect: str, resource: str) -> dict | None:
        """None if the effect may proceed, else a refusal descriptor."""
        p = self.policy
        key = (task_id, effect, resource)
        used = self.consumed.get(key, 0)
        if p.effect_count is not None and used >= p.effect_count:
            return {"reason": f"effect budget exhausted: {effect} on {resource} "
                              f"({used}/{p.effect_count})",
                    "budget": {"scope": "task-effect-resource", "consumed": used,
                               "cap": p.effect_count}}
        total = self.task_total.get(task_id, 0)
        if p.task_effects is not None and total >= p.task_effects:
            return {"reason": f"task effect budget exhausted ({total}/{p.task_effects})",
                    "budget": {"scope": "task", "consumed": total, "cap": p.task_effects}}
        if p.rate_n is not None:
            now = self._wall()
            window = [t for t in self.stamps.get((task_id, effect), []) if now - t < p.rate_seconds]
            if len(window) >= p.rate_n:
                return {"reason": f"effect rate exceeded: {effect} ({len(window)} in "
                                  f"{p.rate_seconds:g}s)",
                        "budget": {"scope": "rate", "consumed": len(window), "cap": p.rate_n,
                                   "seconds": p.rate_seconds}}
        return None

    def consume(self, task_id: str, effect: str, resource: str) -> None:
        key = (task_id, effect, resource)
        self.consumed[key] = self.consumed.get(key, 0) + 1
        self.task_total[task_id] = self.task_total.get(task_id, 0) + 1
        if self.policy.rate_n is not None:
            self.stamps.setdefault((task_id, effect), []).append(self._wall())
        self.assert_invariant()

    def assert_invariant(self) -> None:
        p = self.policy
        for key, n in self.consumed.items():
            if n < 0 or (p.effect_count is not None and n > p.effect_count):
                raise AssertionError(f"resource invariant violated for {key}: {n}")
        for task, n in self.task_total.items():
            if n < 0 or (p.task_effects is not None and n > p.task_effects):
                raise AssertionError(f"resource invariant violated for task {task}: {n}")

    # -- restart ---------------------------------------------------------
    def rebuild(self, rows: list[dict], effect_resource) -> int:
        """Re-derive consumption from effect receipts (trusted ledger). Rate
        stamps are not restored (wall-clock continuity across a restart is
        not meaningful); counts are. Returns the number of effects counted."""
        n = 0
        for r in rows:
            if r.get("kind") != "effect":
                continue
            eff = r.get("effect", {})
            try:
                resource = effect_resource(eff.get("name"), eff.get("params", {}))
            except Exception:
                resource = ""
            key = (r.get("task_id"), eff.get("name"), resource)
            self.consumed[key] = self.consumed.get(key, 0) + 1
            self.task_total[r.get("task_id")] = self.task_total.get(r.get("task_id"), 0) + 1
            n += 1
        return n

    def snapshot(self) -> dict:
        return {"consumed": {"|".join(k): v for k, v in self.consumed.items()},
                "task_total": dict(self.task_total)}
