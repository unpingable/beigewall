"""Capability discovery: the *shape* of requestable authority, nothing more.

Discovery is not authorization. The catalog is derived from the policy table,
the projection registry's schema vocabulary, and the effect handlers' parameter
names. It grants nothing, discloses no corpus value, and cannot confirm whether
a particular resource id exists (policy patterns match names, not objects). It
has a stable schema version and a content hash, and every discovery call is
receipted (`kind="discovery"`) with the policy version shown to the agent.
"""

from dataclasses import asdict

from .authority import PolicyRule, _matches
from .grants import scope_matches
from .receipts import canonical_hash

SCHEMA = "capability-catalog@1"

# Projection schema vocabulary (names, resource shapes, disclosed field paths).
# Asserted against the live registry in tests so it cannot drift.
PROJECTION_SHAPES = {
    "conversation@1": {"resource_shape": "messages/thread/<id>",
                       "fields": ["thread.title", "message.id",
                                  "message.sender_id", "message.sender_label",
                                  "message.ts", "message.body",
                                  "message.quoted_message_id",
                                  "message.attachment_ids"]},
    "conversation@2": {"resource_shape": "messages/thread/<id>",
                       "fields": ["thread.title", "message.id",
                                  "message.sender_id", "message.sender_label",
                                  "message.sender_email", "message.ts",
                                  "message.body", "message.quoted_message_id",
                                  "message.attachment_ids"]},
    "contact@1": {"resource_shape": "contacts/<id>",
                  "fields": ["contact.name", "contact.emails", "contact.tags"]},
    "contact-directory@1": {"resource_shape": "contacts/*",
                            "fields": ["contact.id", "contact.name"]},
    "note@1": {"resource_shape": "notes/<id>",
               "fields": ["note.title", "note.body", "note.links"]},
    "agenda@1": {"resource_shape": "calendar/events",
                 "fields": ["event.title", "event.start", "event.attendees",
                            "event.linked_thread"]},
    "attachment@1": {"resource_shape": "messages/attachment/<id>",
                     "fields": ["attachment.name", "attachment.mime",
                                "attachment.size",
                                "attachment.extracted_text"]},
}

EFFECT_SHAPES = {
    "send_message": {"grant_resource": "messages/thread/<id>",
                     "params": ["thread_id", "body"]},
    "create_event": {"grant_resource": "calendar/events",
                     "params": ["title", "start", "attendees?",
                                "linked_thread?"]},
    "update_note": {"grant_resource": "notes/<id>",
                    "params": ["note_id", "body"]},
}

REQUEST_SCHEMA = {
    "request_grant": {"resource": "scope pattern: exact id path or trailing /*",
                      "operation": "read | send_message | create_event | update_note",
                      "projection": "name@version (read only)",
                      "ttl": "seconds (capped)",
                      "budget": {"reads": "int", "objects": "int"}},
    "read": {"grant_id": "g-N", "resource": "within grant scope",
             "projection": "must equal the grant's projection"},
    "effect": {"grant_id": "g-N", "effect_name": "...", "params": {},
               "derived_from": [{"type": "projection", "ref": "content_hash"},
                                {"type": "user_input", "ref": "prompt_hash"}]},
}


def policy_decision(rules: list[PolicyRule], *, task_id: str, resource: str,
                    operation: str, projection: str | None) -> tuple[str, PolicyRule | None]:
    """Pure replica of the authority's first-match decision (no side effects).
    Used by discovery and by post-hoc negotiation telemetry."""
    rule = next((r for r in rules
                 if _matches(r, task_id=task_id, resource=resource,
                             operation=operation, projection=projection)),
                None)
    if rule is None:
        return "deny", None
    return rule.decision, rule


def policy_version(rules: list[PolicyRule]) -> str:
    return canonical_hash([_rule_dict(r) for r in rules])


def _rule_dict(r: PolicyRule) -> dict:
    d = asdict(r)
    if r.budget_cap is not None:
        d["budget_cap"] = {"reads": r.budget_cap.reads,
                           "objects": r.budget_cap.objects}
    return d


def _entry(r: PolicyRule) -> dict:
    e = {"resource": r.resource, "match": "exact" if r.exact else "pattern",
         "operation": r.operation, "projection": r.projection,
         "decision": r.decision}
    if r.reason:
        e["reason"] = r.reason
    if r.budget_cap is not None:
        e["budget_cap"] = {"reads": r.budget_cap.reads,
                           "objects": r.budget_cap.objects}
    if r.ttl_cap is not None:
        e["ttl_cap"] = r.ttl_cap
    return e


class CapabilityCatalog:
    def __init__(self, rules: list[PolicyRule], *, default_ttl: float,
                 max_ttl: float, default_budget: dict):
        self._rules = list(rules)
        self._defaults = {"ttl": default_ttl, "max_ttl": max_ttl,
                          "budget": dict(default_budget)}
        self.policy_version = policy_version(self._rules)

    def build(self, task_id: str, resource: str | None = None) -> dict:
        """The catalog as shown to one task. `resource` filters the rule list
        to rules that would match that scope; whether a resource *exists* is
        never answered (patterns match names, not objects)."""
        applicable = [r for r in self._rules
                      if r.task_id in ("*", task_id)]
        if resource is not None:
            applicable = [r for r in applicable
                          if r.resource == "*"
                          or (r.resource == resource if r.exact
                              else scope_matches(r.resource, resource))]
        body = {
            "schema": SCHEMA,
            "policy_version": self.policy_version,
            "task_id": task_id,
            "query": resource,
            "query_matched": (None if resource is None else bool(applicable)),
            "default_decision": "deny",
            "rules": [_entry(r) for r in applicable],
            "defaults": self._defaults,
            "projections": PROJECTION_SHAPES,
            "effects": EFFECT_SHAPES,
            "request_schema": REQUEST_SCHEMA,
            "note": "Discovery grants no authority and shows no content. "
                    "Rules are consulted in order; the first match decides; "
                    "no match means deny.",
        }
        body["catalog_hash"] = canonical_hash(body)
        return body
