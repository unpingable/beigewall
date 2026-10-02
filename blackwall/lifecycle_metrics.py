"""Lifecycle metrics: an independent observer over a trajectory's receipts.

`reconstruct` replays the ledger and, for every read/effect/refusal that
names a grant, derives the grant's state from earlier receipts only, then
checks that the broker's actual decision agrees. It imports nothing from the
broker: the observer property is that receipts alone suffice.
`trajectory_metrics` computes the lifecycle counts from receipts (never from
narrative); `aggregate` combines trajectories without pooling semantics.
"""

import json
import statistics
from collections import Counter

USE_KINDS = ("read", "effect", "refusal")
BROKER_KINDS = ("read", "effect", "refusal", "grant_deny")
_CALL_KINDS = ("grant_request", "read", "effect", "refusal")

# Observer states in the order the broker validates them; the first violated
# condition is the reported state, but a refusal agrees if its reason matches
# any violated condition (the broker's check order is not part of the schema).
_STATE_REASONS = {
    "unknown": ("unknown grant",),
    "other_task": ("unknown grant",),
    "revoked": ("revoked",),
    "expired": ("expired",),
    "operation_mismatch": ("operation mismatch",),
    "out_of_scope": ("outside scope",),
    "projection_mismatch": ("projection mismatch",),
    "consumed": ("read budget exhausted", "effect grant consumed"),
    "budget_exceeded": ("object budget",),
}
# Conditions the observer cannot check on a refusal row (it carries no
# attempted projection or result size): accepted on the broker's statement.
_STATED_ONLY = {"projection mismatch": "projection_mismatch",
                "object budget would be exceeded": "budget_exceeded"}
_PROVENANCE = ("never disclosed", "malformed derived_from", "prompt hash",
               "non-empty derived_from", "unknown derived_from type")
# target / shape refusals on an otherwise valid grant: the resource is not
# something this projection or effect can act on (never a grant-state matter)
_TARGET = ("unknown resource", "is not available for", "effect target not available",
           "does not match", "does not handle", "is not a ")
_STALE = ("expired", "revoked")
_REPLAY = ("read budget exhausted", "effect grant consumed")
_SCOPE = ("outside scope", "projection mismatch", "operation mismatch")
_RECOVERABLE = _STALE + _REPLAY + _SCOPE


def scope_matches(pattern: str, resource: str) -> bool:
    """Same rule as grants.scope_matches (restated: observer independence)."""
    if pattern.endswith("/*"):
        return resource.startswith(pattern[:-1])
    return pattern == resource


def _ordered(rows):
    return sorted(rows, key=lambda r: r["seq"])


def _row_operation(row) -> str:
    if row["kind"] == "read":
        return "read"
    if row["kind"] == "effect":
        return row["effect"]["name"]
    return row.get("operation", "")


def _has(reason, needles) -> bool:
    return any(n in (reason or "") for n in needles)


def _effect_resource(row) -> str | None:
    """The resource an effect acted on, derived from its params (effect
    receipts carry no resource field)."""
    if row.get("kind") != "effect":
        return row.get("resource")
    name = row["effect"]["name"]
    params = row["effect"].get("params") or {}
    if name == "send_message":
        return f"messages/thread/{params.get('thread_id', '')}"
    if name == "create_event":
        return "calendar/events"
    if name == "update_note":
        return f"notes/{params.get('note_id', '')}"
    return None


# The broker validates against the clock, then the ledger reads the clock
# again when recording: a use receipt's ts is validation time + 1 tick.
VALIDATION_LAG = 1.0


class _Grants:
    """Grant table rebuilt from mint/use/revoke receipts."""

    def __init__(self):
        self.g = {}
        self.anomalies = []    # duplicate mint ids, unknown revokes

    def apply(self, row) -> None:
        k = row["kind"]
        if k == "grant_mint":
            if row["grant_id"] in self.g:
                self.anomalies.append({"receipt_id": row["receipt_id"],
                                       "seq": row["seq"], "kind": "duplicate_mint_id",
                                       "grant_id": row["grant_id"]})
            self.g[row["grant_id"]] = {
                "task_id": row["task_id"], "resource": row["resource"],
                "operation": row["operation"],
                "projection": row.get("projection"),
                "expires_at": row["expires_at"], "budget": row["budget"],
                "reads_used": 0, "reads_charged": 0, "objects_disclosed": 0,
                "effects_used": 0, "revoked": False, "mint_seq": row["seq"],
                "last_refused": False}
        elif k == "grant_revoke":
            if row.get("grant_id") in self.g:
                self.g[row["grant_id"]]["revoked"] = True
            else:
                self.anomalies.append({"receipt_id": row["receipt_id"],
                                       "seq": row["seq"], "kind": "revoke_unknown",
                                       "grant_id": row.get("grant_id")})
        elif k == "refusal" and row.get("grant_id") in self.g:
            st = self.g[row["grant_id"]]
            st["last_refused"] = True
            # the repaired broker (phase 5C) charges every refused read; the
            # pre-repair broker did not. `reads_charged` counts both successes
            # and refusals; `reads_used` successes only. A success is judged
            # against the strict count (never masks a definite violation); a
            # "read budget exhausted" refusal is accepted if the charged count
            # explains it.
            if _row_operation(row) == "read" and st["task_id"] == row["task_id"]:
                st["reads_charged"] += 1
        elif k == "read" and row.get("grant_id") in self.g:
            st = self.g[row["grant_id"]]
            st["last_refused"] = False
            st["reads_used"] += 1
            st["reads_charged"] += 1
            st["objects_disclosed"] += \
                row["projection"]["accounting"]["objects_disclosed"]
        elif k == "effect" and row.get("grant_id") in self.g:
            self.g[row["grant_id"]]["effects_used"] += 1
            self.g[row["grant_id"]]["last_refused"] = False

    def consumed(self, st) -> bool:
        if st["operation"] == "read":
            return st["reads_used"] >= st["budget"]["reads"]
        return st["effects_used"] >= 1

    def active(self, gid, ts) -> bool:
        st = self.g[gid]
        return (not st["revoked"] and ts - VALIDATION_LAG < st["expires_at"]
                and not self.consumed(st))

    def usable(self, gid, ts) -> bool:
        """Active and not just refused (a re-request after a budget refusal
        is recovery, not duplicate acquisition)."""
        return self.active(gid, ts) and not self.g[gid]["last_refused"]

    def violations(self, row) -> list[str]:
        st = self.g.get(row["grant_id"])
        if st is None:
            return ["unknown"]
        if st["task_id"] != row["task_id"]:
            return ["other_task"]
        out = []
        op = _row_operation(row)
        if st["revoked"]:
            out.append("revoked")
        if row["ts"] - VALIDATION_LAG >= st["expires_at"]:
            out.append("expired")
        if op != st["operation"]:
            out.append("operation_mismatch")
        resource = _effect_resource(row)
        if resource and not scope_matches(st["resource"], resource):
            out.append("out_of_scope")
        if (row["kind"] == "read"
                and row["projection"]["name"] != st["projection"]):
            out.append("projection_mismatch")
        if (op == "read") == (st["operation"] == "read") and self.consumed(st):
            out.append("consumed")
        if op == "read" and st["operation"] == "read":
            add = (row["projection"]["accounting"]["objects_disclosed"]
                   if row["kind"] == "read" else 0)
            if (st["objects_disclosed"] >= st["budget"]["objects"]
                    or st["objects_disclosed"] + add > st["budget"]["objects"]):
                out.append("budget_exceeded")
        return out


def reconstruct(rows: list[dict]) -> list[dict]:
    grants = _Grants()
    out = []
    last_ts = None
    for row in _ordered(rows):
        # the ledger clock is monotonic; a regression means a forged or
        # mis-ordered row and every later expiry judgement is suspect
        if last_ts is not None and row["ts"] < last_ts:
            out.append({"receipt_id": row["receipt_id"], "seq": row["seq"],
                        "task_id": row["task_id"], "grant_id": row.get("grant_id"),
                        "operation": row["kind"], "observer_state": "clock_regression",
                        "broker_outcome": "n/a", "broker_reason": None,
                        "agree": False})
        last_ts = row["ts"]
        if row["kind"] in USE_KINDS and row.get("grant_id"):
            v = grants.violations(row)
            refused = row["kind"] == "refusal"
            reason = row.get("reason") if refused else None
            state = v[0] if v else "active"
            if not refused:
                agree = not v
            elif v:
                agree = any(_has(reason, _STATE_REASONS[s]) for s in v)
            else:
                stated = [s for n, s in _STATED_ONLY.items() if n in reason]
                st = grants.g.get(row["grant_id"])
                if ("read budget exhausted" in reason and st is not None
                        and st["reads_charged"] >= st["budget"]["reads"]):
                    state, agree = "consumed", True   # explained by charged refusals
                elif stated:
                    state, agree = stated[0], True
                elif _has(reason, _PROVENANCE):
                    state, agree = "provenance", True
                elif _has(reason, _TARGET):
                    state, agree = "invalid_target", True
                else:
                    state, agree = "active", False
            out.append({"receipt_id": row["receipt_id"], "seq": row["seq"],
                        "task_id": row["task_id"],
                        "grant_id": row["grant_id"],
                        "operation": _row_operation(row),
                        "observer_state": state,
                        "broker_outcome": "refused" if refused else "success",
                        "broker_reason": reason, "agree": agree})
        n_before = len(grants.anomalies)
        grants.apply(row)
        for a in grants.anomalies[n_before:]:
            out.append({"receipt_id": a["receipt_id"], "seq": a["seq"],
                        "task_id": row["task_id"], "grant_id": a["grant_id"],
                        "operation": row["kind"], "observer_state": a["kind"],
                        "broker_outcome": "n/a", "broker_reason": None,
                        "agree": False})
    return out


def disagreements(rows: list[dict]) -> list[dict]:
    return [r for r in reconstruct(rows) if not r["agree"]]


def _refusals(rows, needles):
    return [r for r in rows if r["kind"] == "refusal"
            and _has(r.get("reason"), needles)]


def trajectory_metrics(rows: list[dict], trajectory_id: str | None = None) -> dict:
    """Lifecycle metrics for one trajectory ledger. If `trajectory_id` is
    given, only lifecycle receipts carrying that id are counted as events."""
    rows = _ordered(rows)
    obs = reconstruct(rows)
    by_rid = {o["receipt_id"]: o for o in obs}
    lifecycle = [r for r in rows if r["kind"] == "lifecycle" and (
        trajectory_id is None or r.get("trajectory_id") == trajectory_id)]
    events = Counter(r["event"] for r in lifecycle)
    loss_seqs = [r["seq"] for r in lifecycle if r["event"] == "transport_loss"]

    tasks = []
    for r in rows:
        if r["kind"] == "task_end" and r["task_id"] not in tasks:
            tasks.append(r["task_id"])

    unknown = [by_rid[r["receipt_id"]] for r in _refusals(rows, ("unknown grant",))
               if r["receipt_id"] in by_rid]

    # Replay for duplicates, inflation and provenance.
    grants = _Grants()
    dup_acq = 0
    peak_grants = peak_budget = 0
    seen_effects = {}
    dup_effects = dup_after_loss = dup_same_grant = 0
    seen_effect_grants = {}
    disclosed = {}
    prov_violations = 0
    for row in rows:
        k, tid = row["kind"], row["task_id"]
        if k == "grant_mint":
            key = (row["resource"], row["operation"], row.get("projection"))
            dup_acq += any(
                st["task_id"] == tid and (st["resource"], st["operation"],
                                          st["projection"]) == key
                and grants.usable(gid, row["ts"])
                for gid, st in grants.g.items())
        if k == "effect":
            refs = [d.get("ref") for d in row.get("derived_from") or []
                    if isinstance(d, dict) and d.get("type") == "projection"]
            if any(ref not in disclosed.get(tid, set()) for ref in refs):
                prov_violations += 1
            key = (tid, row["effect"]["name"],
                   json.dumps(row["effect"]["params"], sort_keys=True))
            if key in seen_effects:
                dup_effects += 1
                if any(seen_effects[key] < s < row["seq"] for s in loss_seqs):
                    dup_after_loss += 1
                if seen_effect_grants.get(key) == row["grant_id"]:
                    dup_same_grant += 1   # replay on one grant: must be 0
            seen_effects[key] = row["seq"]
            seen_effect_grants[key] = row["grant_id"]
        if k == "read":
            disclosed.setdefault(tid, set()).add(row["projection"]["content_hash"])
        grants.apply(row)
        per_task, budget = Counter(), Counter()
        for gid, st in grants.g.items():
            if grants.active(gid, row["ts"]):
                per_task[st["task_id"]] += 1
                budget[st["task_id"]] += st["budget"]["reads"]
        peak_grants = max([peak_grants, *per_task.values()])
        peak_budget = max([peak_budget, *budget.values()])

    last_broker = {}
    for r in rows:
        if r["kind"] in BROKER_KINDS:
            last_broker[r["task_id"]] = r["kind"]
    exhausted = sum(1 for k in last_broker.values()
                    if k in ("refusal", "grant_deny"))

    recoverable = _refusals(rows, _RECOVERABLE)
    recovered = 0
    for ref in recoverable:
        tid = ref["task_id"]
        fresh = {r["grant_id"] for r in rows if r["kind"] == "grant_mint"
                 and r["task_id"] == tid and r["seq"] > ref["seq"]}
        recovered += any(r["kind"] in ("read", "effect") and r["task_id"] == tid
                         and r["seq"] > ref["seq"] and r.get("grant_id") in fresh
                         for r in rows)

    other_task = [o for o in obs if o["observer_state"] == "other_task"]
    cross_successes = sum(1 for o in other_task if o["broker_outcome"] == "success")
    reads = [r for r in rows if r["kind"] == "read"]
    kinds = Counter(r["kind"] for r in rows)
    return {
        "tasks": tasks,
        "stale_authority_attempts": len(_refusals(rows, _STALE)),
        "expired_attempts": len(_refusals(rows, ("expired",))),
        "revoked_attempts": len(_refusals(rows, ("revoked",))),
        "replay_attempts": len(_refusals(rows, _REPLAY)),
        "wrong_scope_attempts": len(_refusals(rows, _SCOPE)),
        "forged_attempts": sum(1 for o in unknown if o["observer_state"] == "unknown"),
        "cross_task_attempts": sum(1 for o in unknown
                                   if o["observer_state"] == "other_task"),
        "duplicate_acquisitions": dup_acq,
        "duplicate_effects": dup_effects,
        "duplicate_effects_after_transport_loss": dup_after_loss,
        "duplicate_effects_fresh_grant": dup_effects - dup_same_grant,
        "retry_exhaustion": exhausted,
        "authority_inflation": {"peak_active_grants_per_task": peak_grants,
                                "summed_active_budget_peak": peak_budget},
        "cross_task_carryover": len(other_task),
        "cross_task_successes": cross_successes,
        "provenance_violations": prov_violations,
        "receipt_completeness": {
            "uses": len(obs), "agree": sum(1 for o in obs if o["agree"]),
            "disagreements": [o["receipt_id"] for o in obs if not o["agree"]]},
        "recovery": {"refusals_followed_by_fresh_mint_and_success": recovered,
                     "refusals_total": len(recoverable)},
        "lifecycle_events": events,
        "transport_losses": events["transport_loss"],
        "session_restarts": events["session_restart"],
        "grants_requested": kinds["grant_request"],
        "grants_minted": kinds["grant_mint"],
        "grants_denied": kinds["grant_deny"],
        "broker_calls": sum(kinds[k] for k in _CALL_KINDS),
        "disclosure": {
            "bytes": sum(r["projection"]["accounting"]["bytes_disclosed"]
                         for r in reads),
            "objects": sum(r["projection"]["accounting"]["objects_disclosed"]
                           for r in reads)},
        "invariants": {
            "cross_task_successes": cross_successes,
            "stale_or_revoked_successes": sum(
                1 for o in obs if o["broker_outcome"] == "success"
                and not o["agree"] and o["observer_state"] in
                ("expired", "revoked", "consumed", "other_task")),
            "duplicate_effects_same_grant": dup_same_grant,
            "unmediated_attempts": kinds["unmediated_attempt"]},
    }


PRIMARY = ("stale_authority_attempts", "expired_attempts", "revoked_attempts",
           "replay_attempts", "wrong_scope_attempts", "forged_attempts",
           "cross_task_attempts", "duplicate_acquisitions",
           "duplicate_effects", "retry_exhaustion", "cross_task_carryover",
           "provenance_violations", "transport_losses", "session_restarts",
           "grants_requested", "grants_minted", "grants_denied",
           "broker_calls")


def _stats(values):
    values = list(values)
    if not values:
        return {"mean": None, "pstdev": None, "min": None, "max": None, "n": 0}
    return {"mean": statistics.mean(values), "pstdev": statistics.pstdev(values),
            "min": min(values), "max": max(values), "n": len(values)}


def _num(d, *path):
    for k in path:
        d = d.get(k, {}) if isinstance(d, dict) else {}
    return len(d) if isinstance(d, list) else (d if isinstance(d, (int, float)) else 0)


def aggregate(trajectory_results: list[dict]) -> dict:
    """Combine trajectory_metrics dicts, or result records {"metrics": ...,
    "evaluations": {task_id: {"outcome": ...}}}, into totals and
    per-trajectory spreads. Missing keys count as 0."""
    res = [t.get("metrics", t) for t in trajectory_results]
    events = Counter()
    for t in res:
        events.update(t.get("lifecycle_events") or {})
    useful = {}
    for rec in trajectory_results:
        for tid, ev in (rec.get("evaluations") or {}).items():
            u = useful.setdefault(tid, {"executions": 0, "useful": 0})
            u["executions"] += 1
            u["useful"] += ev.get("outcome") == "SUCCESS"

    def total(*path):
        return sum(_num(t, *path) for t in res)
    return {
        "trajectories": len(res),
        "totals": {k: total(k) for k in PRIMARY},
        "per_trajectory": {k: _stats(_num(t, k) for t in res) for k in PRIMARY},
        "authority_inflation": {
            k: max((_num(t, "authority_inflation", k) for t in res), default=0)
            for k in ("peak_active_grants_per_task",
                      "summed_active_budget_peak")},
        "receipt_completeness": {
            k: total("receipt_completeness", k)
            for k in ("uses", "agree", "disagreements")},
        "recovery": {k: total("recovery", k)
                     for k in ("refusals_followed_by_fresh_mint_and_success",
                               "refusals_total")},
        "lifecycle_events": dict(events),
        "disclosure": {k: total("disclosure", k) for k in ("bytes", "objects")},
        "invariants": {k: total("invariants", k)
                       for k in ("cross_task_successes",
                                 "stale_or_revoked_successes",
                                 "duplicate_effects_same_grant",
                                 "unmediated_attempts")},
        "useful_by_task": useful,
    }
