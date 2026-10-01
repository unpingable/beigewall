"""Capability broker: the agent's only tool surface.

Every operation is receipted. Validation is fail-closed: any failed check
records a `refusal` receipt and raises RefusalError. Grant denials are normal
control flow and return {"ok": False, ...} with a `grant_deny` receipt.

Effect calls must carry dependency provenance (`derived_from`): projection
refs must match content hashes this task actually received, and user_input
refs must match the task's registered prompt hash.
"""

from .authority import PolicyAuthority
from .capabilities import CapabilityCatalog
from .corpus import Corpus
from .effects import EFFECTS, EffectError
from .grants import Budget, Grant, GrantError, validate_use
from .projections import ProjectionError, run_projection
from .receipts import Ledger

DEFAULT_TTL = 600.0
DEFAULT_BUDGET = Budget(reads=8, objects=200)


class RefusalError(Exception):
    def __init__(self, message: str, receipt_id: str):
        super().__init__(message)
        self.receipt_id = receipt_id


def _effect_resource(effect_name: str, params: dict) -> str:
    """Map an effect call to the resource scope it must be granted on."""
    if effect_name not in EFFECTS:
        raise GrantError(f"unknown effect {effect_name!r}")
    if effect_name == "send_message":
        return f"messages/thread/{params.get('thread_id', '')}"
    if effect_name == "create_event":
        return "calendar/events"
    if effect_name == "update_note":
        return f"notes/{params.get('note_id', '')}"
    raise GrantError(f"no resource mapping for {effect_name!r}")


class CapabilityBroker:
    def __init__(self, corpus: Corpus, authority: PolicyAuthority,
                 ledger: Ledger, clock):
        self._corpus = corpus
        self._authority = authority
        self._ledger = ledger
        self._clock = clock
        self._grants: dict[str, Grant] = {}
        self._task_prompts: dict[str, str] = {}
        self._catalog = CapabilityCatalog(
            authority.rules, default_ttl=DEFAULT_TTL, max_ttl=authority.max_ttl,
            default_budget={"reads": DEFAULT_BUDGET.reads,
                            "objects": DEFAULT_BUDGET.objects})

    def begin_task(self, task_id: str, prompt_hash: str) -> None:
        self._task_prompts[task_id] = prompt_hash

    # --- agent surface -------------------------------------------------

    def request_grant(self, task_id: str, resource: str, operation: str,
                      projection: str | None = None, ttl: float = DEFAULT_TTL,
                      budget: Budget = DEFAULT_BUDGET) -> dict:
        self._ledger.record("grant_request", task_id, resource=resource,
                            operation=operation, projection=projection,
                            ttl=ttl, budget={"reads": budget.reads,
                                             "objects": budget.objects})
        grant, reason = self._authority.decide(
            task_id=task_id, resource=resource, operation=operation,
            projection=projection, ttl=ttl, budget=budget)
        if grant is None:
            self._ledger.record("grant_deny", task_id, resource=resource,
                                operation=operation, projection=projection,
                                reason=reason)
            return {"ok": False, "reason": reason}
        self._grants[grant.grant_id] = grant
        self._ledger.record("grant_mint", task_id, grant_id=grant.grant_id,
                            resource=grant.resource, operation=grant.operation,
                            projection=grant.projection,
                            expires_at=grant.expires_at,
                            budget={"reads": grant.budget.reads,
                                    "objects": grant.budget.objects})
        return {"ok": True, "grant_id": grant.grant_id, "expires_at": grant.expires_at,
                "budget": {"reads": grant.budget.reads, "objects": grant.budget.objects}}

    def discover(self, task_id: str, resource: str | None = None) -> dict:
        """Capability discovery: the shape of requestable authority. Grants
        nothing, discloses no corpus value, touches no grant state. Receipted
        as `discovery` with the policy/catalog version shown."""
        catalog = self._catalog.build(task_id, resource)
        receipt = self._ledger.record(
            "discovery", task_id, schema=catalog["schema"],
            policy_version=catalog["policy_version"],
            catalog_hash=catalog["catalog_hash"], query=resource,
            query_matched=catalog["query_matched"],
            rules_shown=len(catalog["rules"]))
        return {"ok": True, "receipt_id": receipt.receipt_id,
                "catalog": catalog}

    def read(self, task_id: str, grant_id: str, resource: str,
             projection: str) -> dict:
        grant = self._grant_or_refuse(task_id, grant_id, operation="read",
                                      resource=resource)
        try:
            validate_use(grant, operation="read", resource=resource,
                         projection=projection, now=self._clock())
            result = run_projection(projection, self._corpus, resource)
            new_objects = grant.objects_disclosed + result.accounting["objects_disclosed"]
            if new_objects > grant.budget.objects:
                raise GrantError(
                    f"grant {grant_id} object budget would be exceeded: "
                    f"{new_objects} > {grant.budget.objects}")
        except (GrantError, ProjectionError) as exc:
            raise self._refuse(task_id, "read", grant_id, resource, str(exc))
        except KeyError as exc:
            raise self._refuse(task_id, "read", grant_id, resource,
                               f"unknown resource: {exc}")

        grant.reads_used += 1
        grant.objects_disclosed += result.accounting["objects_disclosed"]
        meta = {
            "name": result.projection,
            "content_hash": result.content_hash,
            "source_object_ids": result.source_object_ids,
            "disclosed_fields": result.disclosed_fields,
            "accounting": result.accounting,
        }
        receipt = self._ledger.record("read", task_id, grant_id=grant_id,
                                      resource=resource, projection=meta)
        return {"ok": True, "receipt_id": receipt.receipt_id,
                "content": result.content, "projection_meta": meta}

    def effect(self, task_id: str, grant_id: str, effect_name: str,
               params: dict, derived_from: list[dict]) -> dict:
        try:
            resource = _effect_resource(effect_name, params)
        except GrantError as exc:
            raise self._refuse(task_id, effect_name, grant_id, "", str(exc))
        grant = self._grant_or_refuse(task_id, grant_id, operation=effect_name,
                                      resource=resource)
        try:
            validate_use(grant, operation=effect_name, resource=resource,
                         projection=None, now=self._clock())
            self._validate_provenance(task_id, derived_from)
        except GrantError as exc:
            raise self._refuse(task_id, effect_name, grant_id, resource, str(exc))
        handler = EFFECTS[effect_name]
        try:
            summary = handler(self._corpus, params)
        except (KeyError, EffectError) as exc:
            raise self._refuse(task_id, effect_name, grant_id, resource,
                               f"unknown resource or invalid effect target: {exc}")
        receipt = self._ledger.record("effect", task_id, grant_id=grant_id,
                                      effect={"name": effect_name, "params": params},
                                      derived_from=derived_from,
                                      result_summary=summary)
        return {"ok": True, "receipt_id": receipt.receipt_id, "summary": summary}

    # --- internals -------------------------------------------------------

    def _grant_or_refuse(self, task_id: str, grant_id: str, *, operation: str,
                         resource: str) -> Grant:
        grant = self._grants.get(grant_id)
        if grant is None or grant.task_id != task_id:
            raise self._refuse(task_id, operation, grant_id, resource,
                               f"unknown grant {grant_id!r}")
        return grant

    def _refuse(self, task_id: str, operation: str, grant_id: str,
                resource: str, reason: str) -> RefusalError:
        receipt = self._ledger.record("refusal", task_id, operation=operation,
                                      grant_id=grant_id, resource=resource,
                                      reason=reason)
        return RefusalError(reason, receipt.receipt_id)

    def _validate_provenance(self, task_id: str, derived_from: list[dict]) -> None:
        if not derived_from:
            raise GrantError("effect requires non-empty derived_from provenance")
        disclosed_hashes = {
            r.detail["projection"]["content_hash"]
            for r in self._ledger.by_task(task_id) if r.kind == "read"
        }
        prompt_hash = self._task_prompts.get(task_id)
        for dep in derived_from:
            if not isinstance(dep, dict) or "type" not in dep or "ref" not in dep:
                raise GrantError(
                    "malformed derived_from entry: expected "
                    '{"type": "projection"|"user_input", "ref": str}')
            dtype, ref = dep.get("type"), dep.get("ref")
            if dtype == "projection":
                if ref not in disclosed_hashes:
                    raise GrantError(
                        "derived_from projection ref was never disclosed "
                        f"to this task: {str(ref)[:16]}")
            elif dtype == "user_input":
                if ref != prompt_hash:
                    raise GrantError("derived_from user_input ref does not match "
                                     "the task prompt hash")
            else:
                raise GrantError(f"unknown derived_from type {dtype!r}")
