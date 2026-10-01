"""Simulated human authority: policy-driven grant decisions.

Every grant request is an authorization event. Rules are consulted in order;
the first match wins; absence of a matching rule denies (fail-closed).
Approval may narrow the requested TTL and budget. The authority holds no
permanent authorization state — its output is always a bounded Grant or a
denial reason.
"""

from dataclasses import dataclass

from .grants import Budget, Grant, scope_matches


@dataclass(frozen=True)
class PolicyRule:
    task_id: str                 # exact task id or "*"
    resource: str                # scope pattern or "*"
    operation: str               # operation or "*"
    projection: str | None       # "name@version" or "*"
    decision: str                # "approve" | "deny"
    ttl_cap: float | None = None
    budget_cap: Budget | None = None
    reason: str = ""
    exact: bool = False          # match resource literally, not as a pattern


def _matches(rule: PolicyRule, *, task_id, resource, operation, projection) -> bool:
    if rule.task_id != "*" and rule.task_id != task_id:
        return False
    if rule.resource != "*":
        if rule.exact:
            if rule.resource != resource:
                return False
        elif not scope_matches(rule.resource, resource):
            return False
    if rule.operation != "*" and rule.operation != operation:
        return False
    if rule.projection != "*" and rule.projection != projection:
        return False
    return True


class PolicyAuthority:
    def __init__(self, rules: list[PolicyRule], clock, max_ttl: float = 900.0):
        self._rules = list(rules)
        self._clock = clock
        self._max_ttl = max_ttl
        self._grant_seq = 0
        self.authorization_events = 0
        self.grants_minted = 0
        self.grants_denied = 0

    @property
    def rules(self) -> list[PolicyRule]:
        """Read-only view of the policy table (for capability discovery and
        post-hoc telemetry). Discovery never consults this at decision time."""
        return list(self._rules)

    @property
    def max_ttl(self) -> float:
        return self._max_ttl

    def decide(self, *, task_id: str, resource: str, operation: str,
               projection: str | None, ttl: float,
               budget: Budget) -> tuple[Grant | None, str]:
        self.authorization_events += 1
        rule = next((r for r in self._rules
                     if _matches(r, task_id=task_id, resource=resource,
                                 operation=operation, projection=projection)),
                    None)
        if rule is None or rule.decision != "approve":
            self.grants_denied += 1
            return None, rule.reason if rule else "no policy rule (default deny)"

        ttl = min(ttl, self._max_ttl)
        if rule.ttl_cap is not None:
            ttl = min(ttl, rule.ttl_cap)
        if rule.budget_cap is not None:
            budget = Budget(reads=min(budget.reads, rule.budget_cap.reads),
                            objects=min(budget.objects, rule.budget_cap.objects))
        self._grant_seq += 1
        self.grants_minted += 1
        grant = Grant(
            grant_id=f"g-{self._grant_seq}",
            task_id=task_id,
            resource=resource,
            operation=operation,
            projection=projection if operation == "read" else None,
            expires_at=self._clock() + ttl,
            budget=budget,
        )
        return grant, rule.reason or "approved"
