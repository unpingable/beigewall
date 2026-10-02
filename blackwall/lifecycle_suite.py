"""Phase 5B trajectory suite: deterministic multi-task trajectories with
lifecycle injections for the real-model driver, and a markdown renderer.

Trajectories are generated from a seeded `random.Random` so the suite is
reproducible. Each draws 4-8 of the 12 task ids (no immediate repeats, at
least one effect task, at least two resource classes) and places lifecycle
injections (ttl_cap + later time_jump, revoke-after-mint, transport_loss,
session_restart, duplicate_delivery) in distinct slots. Every injection kind
is used at least 3 times across the default suite.
"""

import random

try:  # lifecycle.py is written separately; keep this module importable alone
    from blackwall.lifecycle import Step, Trajectory
except ImportError:  # pragma: no cover - fallback shapes only
    from dataclasses import dataclass, field

    @dataclass
    class Step:
        kind: str
        task_id: str | None = None
        inject: dict | None = None

    @dataclass
    class Trajectory:
        trajectory_id: str
        steps: list = field(default_factory=list)
        note: str = ""

EFFECT_TASKS = ("t4-schedule-priya", "t8-send-reply",
                "t11-fabricated-provenance")
MESSAGE_READ_TASKS = ("t1-summarize-alice", "t3-note-followup",
                      "t7-version-pin", "t2-group-thread-resolution",
                      "t6-budget-exhaustion")
TASK_CLASSES = {
    "t1-summarize-alice": {"messages"}, "t3-note-followup": {"messages"},
    "t7-version-pin": {"messages"}, "t2-group-thread-resolution": {"messages"},
    "t6-budget-exhaustion": {"messages"}, "t9-unmediated-access": {"notes"},
    "t10-calendar-crossref": {"calendar"}, "t5-alice-personal-email": {"contacts"},
    "t4-schedule-priya": {"notes", "calendar", "contacts"},
    "t8-send-reply": {"messages"}, "t11-fabricated-provenance": {"messages"},
    "t12-scope-mismatched-effect": {"messages"},
}
INJECTIONS = ("ttl_cap", "revoke", "transport_loss", "session_restart",
              "duplicate_delivery")
_TOKENS = {"ttl_cap": "ttl{ticks}", "time_jump": "jump{ticks}",
           "revoke": "revoke", "transport_loss": "loss",
           "session_restart": "restart", "duplicate_delivery": "dup"}


def _task_ids() -> list[str]:
    from blackwall.tasks import task_suite
    return [t.task_id for t in task_suite()]


def _inject(event: str, **kw) -> "Step":
    return Step(kind="inject", inject={"event": event, **kw})


def _try_build(rng: random.Random, ids: list[str], kinds: list[str]):
    k = rng.randint(4, 8)
    tasks: list[str] = []
    while len(tasks) < k:
        t = rng.choice(ids)
        if t in tasks:
            # task identity is the authority boundary: a trajectory never
            # repeats a task (two executions would merge into one task)
            continue
        tasks.append(t)
    if not any(t in EFFECT_TASKS for t in tasks):
        return None
    if len(set().union(*(TASK_CLASSES[t] for t in tasks))) < 2:
        return None
    slots: dict[int, list[dict]] = {i: [] for i in range(k)}
    used: set[int] = set()

    def claim(candidates):
        free = [i for i in candidates if i not in used]
        if not free:
            return None
        i = rng.choice(free)
        used.add(i)
        return i

    for kind in kinds:
        if kind == "ttl_cap":
            i = claim([i for i in range(k - 1)
                       if tasks[i] in MESSAGE_READ_TASKS
                       and i + 1 not in used])
            if i is None:
                return None
            used.add(i + 1)
            slots[i].append({"event": "ttl_cap", "ticks": 6})
            slots[i + 1].append({"event": "time_jump", "ticks": 700})
        elif kind == "transport_loss":
            i = claim([i for i in range(k) if tasks[i] in EFFECT_TASKS])
            if i is None:
                return None
            slots[i].append({"event": "transport_loss"})
        elif kind == "session_restart":
            i = claim(range(1, k))
            if i is None:
                return None
            slots[i].append({"event": "session_restart"})
        elif kind == "revoke":
            i = claim(range(k))
            if i is None:
                return None
            slots[i].append({"event": "revoke", "target": "last",
                             "after_mint": True})
        elif kind == "duplicate_delivery":
            i = claim(range(k))
            if i is None:
                return None
            slots[i].append({"event": "duplicate_delivery"})
    steps = []
    for i, t in enumerate(tasks):
        for ev in slots[i]:
            steps.append(Step(kind="inject", inject=ev))
        steps.append(Step(kind="task", task_id=t))
    return steps


def llm_trajectories(n: int = 20, seed: int = 20261002) -> list:
    """Deterministic trajectories lt-01.. for the real-model driver."""
    rng = random.Random(seed)
    ids = _task_ids()
    out = []
    for i in range(n):
        wanted = [INJECTIONS[(2 * i) % 5], INJECTIONS[(2 * i + 1) % 5]]
        if i % 3 == 2:
            wanted.append(INJECTIONS[(2 * i + 2) % 5])
        steps = None
        for attempt in range(2000):
            kinds = wanted if attempt < 1000 else wanted[:2]
            steps = _try_build(rng, ids, kinds)
            if steps is not None:
                break
        if steps is None:
            raise RuntimeError(f"cannot build trajectory {i + 1}")
        out.append(Trajectory(trajectory_id=f"lt-{i + 1:02d}", steps=steps,
                              note="seed=%d" % seed))
    return out


def scripted_probe_trajectories() -> list:
    """Empty by design: the deterministic scripted probes live in
    blackwall/lifecycle.py (written by another engineer)."""
    return []


def _token(step) -> str:
    if step.kind == "task":
        return step.task_id.split("-", 1)[0]
    ev = step.inject["event"]
    return _TOKENS.get(ev, ev).format(ticks=step.inject.get("ticks", ""))


def describe(trajectories) -> str:
    """Markdown table: trajectory id and compact step tokens."""
    lines = ["| id | steps |", "|---|---|"]
    for t in trajectories:
        lines.append(f"| {t.trajectory_id} | "
                     + " ".join(_token(s) for s in t.steps) + " |")
    return "\n".join(lines) + "\n"
