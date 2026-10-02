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
    effects_used: int = 0      # an effect grant permits one successful effect
    revoked: bool = False      # set by the authority via the broker; terminal


def scope_matches(pattern: str, resource: str) -> bool:
    """Exact match, or trailing `/*` matching strict descendants only."""
    if pattern.endswith("/*"):
        return resource.startswith(pattern[:-1])
    return pattern == resource


def validate_use(grant: Grant, *, operation: str, resource: str,
                 projection: str | None, now: float) -> None:
    """Fail-closed validation of one attempted use. Raises GrantError."""
    if grant.revoked:
        raise GrantError(f"grant {grant.grant_id} revoked")
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
    else:
        # lifecycle (phase 5B): an effect grant is consumed by its first
        # successful effect, so a retry after a lost result cannot land the
        # effect twice; refused attempts do not consume
        if grant.effects_used >= 1:
            raise GrantError(f"grant {grant.grant_id} effect grant consumed")
