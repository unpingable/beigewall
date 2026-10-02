"""Capability broker: the agent's only tool surface.

Every operation is receipted. Validation is fail-closed: any failed check
records a `refusal` receipt and raises RefusalError. Grant denials are normal
control flow and return {"ok": False, ...} with a `grant_deny` receipt.

Effect calls must carry dependency provenance (`derived_from`): projection
refs must match content hashes this task actually received, and user_input
refs must match the task's registered prompt hash.
"""

from .authority import PolicyAuthority
from .capabilities import CapabilityCatalog, resource_class
from .corpus import Corpus
from .effects import EFFECTS, EffectError
from .grants import Budget, Grant, GrantError, validate_use
from .projections import ProjectionError, run_projection
from .receipts import Ledger, LedgerError

DEFAULT_TTL = 600.0
DEFAULT_BUDGET = Budget(reads=8, objects=200)


DISCOVERY_MODES = (None, "global", "denial", "task")


class RefusalError(Exception):
    def __init__(self, message: str, receipt_id: str, hint: dict | None = None):
        super().__init__(message)
        self.receipt_id = receipt_id
        self.hint = hint   # denial-triggered capability hint (receipted)
        # phase 6C: a retry on a consumed effect grant learns the outcome of
        # the effect that consumed it (effect_id, receipt_id, status) rather
        # than nothing — so a retry after a lost response needs no new grant
        self.prior_effect: dict | None = None


class EvidenceError(Exception):
    """The effect did NOT occur: its intent could not be made durable, and
    the broker will not change the world without evidence (phase 6C)."""


class IndeterminateEffect(Exception):
    """The effect MAY have occurred: the handler ran but the commit receipt
    could not be written. Nothing the broker can say is authoritative
    beyond the durable intent; recovery on restart records the effect as
    indeterminate and counts it as consumed (phase 6C)."""

    def __init__(self, effect_id: str, grant_id: str):
        super().__init__(f"effect {effect_id} indeterminate: commit receipt not written")
        self.effect_id = effect_id
        self.grant_id = grant_id


def effect_resource(effect_name: str, params: dict) -> str:
    """Public alias of the effect→resource mapping (used by the resource
    ledger's rebuild and by observers)."""
    return _effect_resource(effect_name, params)


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
                 ledger: Ledger, clock, discovery_mode: str | None = None,
                 resources=None, durable_effects: bool = False):
        if discovery_mode not in DISCOVERY_MODES:
            raise ValueError(f"unknown discovery mode {discovery_mode!r}")
        # phase 6B: optional trusted resource ledger constraining consumption
        # of already-authorized effects; None (default) = unbounded, as before
        self._resources = resources
        # phase 6C: with durable_effects (the trusted server's mode) every
        # effect is bracketed by a durable `effect_intent` receipt before the
        # handler and an `effect` commit receipt after it, both carrying a
        # server-minted effect_id; a handler failure aborts the intent with
        # the refusal receipt. Off (default): the frozen in-process
        # semantics — one `effect` receipt after the handler.
        self._durable = durable_effects
        self._effect_seq = 0
        self._effects_by_grant: dict[str, dict] = {}   # grant_id -> prior_effect
        self._corpus = corpus
        self._authority = authority
        self._ledger = ledger
        self._clock = clock
        self._discovery_mode = discovery_mode
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
            result = {"ok": False, "reason": reason}
            if self._discovery_mode == "denial":
                result["capability_hint"] = self._denial_hint(
                    task_id, resource, operation, reason)
            return result
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
            "discovery", task_id, schema=catalog["schema"], trigger="global",
            policy_version=catalog["policy_version"],
            catalog_hash=catalog["catalog_hash"], query=resource,
            query_matched=catalog["query_matched"],
            scope=sorted({resource_class(r["resource"]) for r in catalog["rules"]
                          if r["resource"] != "*"}),
            rules_shown=len(catalog["rules"]))
        return {"ok": True, "receipt_id": receipt.receipt_id,
                "catalog": catalog}

    def task_view(self, task_id: str, prompt: str) -> dict:
        """Task-conditioned capability view, receipted as an initial
        discovery event. Grants nothing."""
        view = self._catalog.task_view(task_id, prompt)
        receipt = self._ledger.record(
            "discovery", task_id, schema=view["schema"], trigger="initial",
            policy_version=view["policy_version"],
            catalog_hash=view["catalog_hash"], query=None,
            query_matched=None, scope=view["scope"],
            rules_shown=len(view["rules"]))
        return {"receipt_id": receipt.receipt_id, "view": view}

    def _denial_hint(self, task_id: str, resource: str, operation: str,
                     reason: str) -> dict:
        hint = self._catalog.denial_view(task_id, resource, operation, reason)
        receipt = self._ledger.record(
            "discovery", task_id, schema=hint["schema"], trigger="denial",
            policy_version=hint["policy_version"],
            catalog_hash=hint["catalog_hash"], query=resource,
            query_matched=bool(hint["rules"]), scope=hint["scope"],
            rules_shown=len(hint["rules"]))
        hint["receipt_id"] = receipt.receipt_id
        return hint

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
        except GrantError as exc:
            # interface / own-authority refusals (scope, projection, budget,
            # expiry, revocation): the reason may be specific. Every refused
            # read costs one read, so probing is bounded by the approved budget.
            grant.reads_used += 1
            raise self._refuse(task_id, "read", grant_id, resource, str(exc))
        except (ProjectionError, KeyError):
            # protected-state refusals: an unknown id and an id of another
            # type must be indistinguishable (refusal-reason oracle, phase 5C)
            grant.reads_used += 1
            raise self._refuse(task_id, "read", grant_id, resource,
                               f"{projection} is not available for {resource}")

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

    def resume_effect_seq(self, highest: int) -> None:
        """Continue effect ids after the highest id in a prior ledger."""
        self._effect_seq = max(self._effect_seq, highest)

    def prior_effect(self, grant_id: str) -> dict | None:
        return self._effects_by_grant.get(grant_id)

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
            refusal = self._refuse(task_id, effect_name, grant_id, resource, str(exc))
            if "effect grant consumed" in str(exc):
                refusal.prior_effect = self._effects_by_grant.get(grant_id)
            raise refusal
        if self._resources is not None:
            # resource budget: checked after authorization and provenance,
            # before the world changes; a budget refusal never widens or
            # narrows authority and does not consume the grant
            over = self._resources.check(task_id, effect_name, resource)
            if over is not None:
                receipt = self._ledger.record("refusal", task_id, operation=effect_name,
                                              grant_id=grant_id, resource=resource,
                                              reason=over["reason"], budget=over["budget"])
                raise RefusalError(over["reason"], receipt.receipt_id)
        effect_id = None
        if self._durable:
            # 1. durable intent BEFORE the world changes: the only evidence
            #    that survives a crash between here and the commit. If it
            #    cannot be written the effect does not happen.
            self._effect_seq += 1
            effect_id = f"e-{self._effect_seq}"
            try:
                self._ledger.record("effect_intent", task_id, _durable=True,
                                    effect_id=effect_id, grant_id=grant_id,
                                    resource=resource,
                                    effect={"name": effect_name, "params": params},
                                    derived_from=derived_from)
            except LedgerError as exc:
                self._effect_seq -= 1
                raise EvidenceError(f"evidence unavailable: {exc}") from exc
        handler = EFFECTS[effect_name]
        try:
            summary = handler(self._corpus, params)
        except (KeyError, EffectError):
            # unknown target and wrong-typed target are indistinguishable;
            # the handler changes nothing before it can fail, so the
            # intent is aborted (the refusal receipt names the effect_id)
            raise self._refuse(task_id, effect_name, grant_id, resource,
                               "effect target not available", effect_id=effect_id)
        # 2. the world has changed: from here on nothing may be undone;
        #    consumption and the grant advance before the commit so that a
        #    failure leaves them ahead of the evidence, never behind it
        if self._resources is not None:
            self._resources.consume(task_id, effect_name, resource)
        grant.effects_used += 1
        commit = {"grant_id": grant_id, "effect": {"name": effect_name, "params": params},
                  "derived_from": derived_from, "result_summary": summary}
        if effect_id is not None:
            commit["effect_id"] = effect_id
        try:
            receipt = self._ledger.record("effect", task_id, _durable=self._durable, **commit)
        except LedgerError:
            # 3. the commit could not be written: the effect is indeterminate
            #    to everyone but this process. The durable intent is the
            #    record; recovery resolves it. The grant stays consumed.
            self._effects_by_grant[grant_id] = {"effect_id": effect_id, "receipt_id": None,
                                                "status": "indeterminate"}
            raise IndeterminateEffect(effect_id or "", grant_id)
        self._effects_by_grant[grant_id] = {"effect_id": effect_id,
                                            "receipt_id": receipt.receipt_id,
                                            "status": "committed"}
        out = {"ok": True, "receipt_id": receipt.receipt_id, "summary": summary}
        if effect_id is not None:
            out["effect_id"] = effect_id
        return out

    def revoke(self, task_id: str, grant_id: str, reason: str) -> dict:
        """Authority-side revocation of a live grant (phase 5B). Terminal:
        every later use is refused "revoked". Receipted as `grant_revoke`.
        Revoking an unknown or other-task grant is itself a refusal."""
        grant = self._grant_or_refuse(task_id, grant_id, operation="revoke",
                                      resource="")
        grant.revoked = True
        receipt = self._ledger.record("grant_revoke", task_id, grant_id=grant_id,
                                      reason=reason)
        return {"ok": True, "receipt_id": receipt.receipt_id, "grant_id": grant_id}

    # --- internals -------------------------------------------------------

    def _grant_or_refuse(self, task_id: str, grant_id: str, *, operation: str,
                         resource: str) -> Grant:
        grant = self._grants.get(grant_id)
        if grant is None or grant.task_id != task_id:
            raise self._refuse(task_id, operation, grant_id, resource,
                               f"unknown grant {grant_id!r}")
        return grant

    def _refuse(self, task_id: str, operation: str, grant_id: str,
                resource: str, reason: str, effect_id: str | None = None) -> RefusalError:
        extra = {}
        if effect_id is not None:
            extra["effect_id"] = effect_id      # this refusal aborts that intent
        receipt = self._ledger.record("refusal", task_id, operation=operation,
                                      grant_id=grant_id, resource=resource,
                                      reason=reason, _durable=effect_id is not None, **extra)
        hint = None
        if self._discovery_mode == "denial" and resource:
            hint = self._denial_hint(task_id, resource, operation, reason)
        return RefusalError(reason, receipt.receipt_id, hint)

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
