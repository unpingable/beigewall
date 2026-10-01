"""Grant model: scoped, expiring, budgeted capability tokens.

Grants are the only authorization state in the system. They are minted by the
authority, held by the broker, and consumed by use. There is no permanent
authorization state: every grant has an expiry and a cumulative budget.
"""

from dataclasses import dataclass, field


class GrantError(Exception):
    """A grant failed validation for an attempted use."""


@dataclass(frozen=True)
class Budget:
    reads: int
    objects: int


@dataclass
class Grant:
    grant_id: str
    task_id: str
    resource: str
    operation: str
    projection: str | None
    expires_at: float
    budget: Budget
    reads_used: int = 0
    objects_disclosed: int = 0


def scope_matches(pattern: str, resource: str) -> bool:
    """Exact match, or trailing `/*` matching strict descendants only."""
    if pattern.endswith("/*"):
        return resource.startswith(pattern[:-1])
    return pattern == resource


def validate_use(grant: Grant, *, operation: str, resource: str,
                 projection: str | None, now: float) -> None:
    """Fail-closed validation of one attempted use. Raises GrantError."""
    if now >= grant.expires_at:
        raise GrantError(f"grant {grant.grant_id} expired")
    if grant.operation != operation:
        raise GrantError(
            f"grant {grant.grant_id} operation mismatch: "
            f"grant={grant.operation} attempted={operation}")
    if not scope_matches(grant.resource, resource):
        raise GrantError(
            f"grant {grant.grant_id} resource outside scope: "
            f"grant={grant.resource} attempted={resource}")
    if operation == "read":
        if grant.projection is None or grant.projection != projection:
            raise GrantError(
                f"grant {grant.grant_id} projection mismatch: "
                f"grant={grant.projection} attempted={projection}")
        if grant.reads_used >= grant.budget.reads:
            raise GrantError(f"grant {grant.grant_id} read budget exhausted")
        if grant.objects_disclosed >= grant.budget.objects:
            raise GrantError(f"grant {grant.grant_id} object budget exhausted")
