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
HINT_SCHEMA = "capability-hint@1"      # denial-triggered, scoped to one class
VIEW_SCHEMA = "capability-view@1"      # task-conditioned, scoped by grounding

# Resource classes are the first path segment of a scope. Grounding a task
# prompt to classes uses a fixed lexicon (the same for every task) plus one
# generic rule: a capitalised, non-sentence-initial token that is not a
# weekday/month is a person reference, which grounds "contacts". This is a
# mechanism, not a per-task hint; it was written with knowledge of the task
# wording and is listed as a limitation in FINDINGS_PHASE5.md.
GROUNDING_LEXICON = {
    "messages": ("thread", "threads", "message", "messages", "reply",
                 "replies", "post", "conversation", "sent", "send",
                 "attachment", "attachments", "draft", "file", "pdf"),
    "notes": ("note", "notes"),
    "calendar": ("meeting", "meetings", "calendar", "schedule", "scheduled",
                 "event", "events", "coffee", "appointment"),
    "contacts": ("contact", "contacts", "email", "emails", "address",
                 "addresses", "who", "person", "people", "participant",
                 "participants", "directory"),
}
_NOT_NAMES = {"monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday", "january", "february", "march", "april",
              "may", "june", "july", "august", "september", "october",
              "november", "december", "i"}


KNOWN_CLASSES = ("messages", "notes", "contacts", "calendar")


def resource_class(resource: str) -> str:
    """First path segment, clamped to the known classes so arbitrary input
    never reaches a receipt as a 'class'."""
    cls = (resource or "").split("/", 1)[0]
    return cls if cls in KNOWN_CLASSES else "other"


def ground_prompt(prompt: str) -> dict:
    """Resource classes a task prompt refers to, with the matching terms."""
    import re
    classes: dict[str, list] = {}
    sentence_start = True
    for raw in re.findall(r"[A-Za-z][A-Za-z'-]*|[.!?]", prompt):
        if raw in ".!?":
            sentence_start = True
            continue
        tok = raw.lower().removesuffix("'s")
        for cls, words in GROUNDING_LEXICON.items():
            if tok in words:
                classes.setdefault(cls, []).append(raw)
        if (raw[0].isupper() and len(raw) > 1 and not sentence_start
                and tok not in _NOT_NAMES and tok not in sum(
                    GROUNDING_LEXICON.values(), ())):
            classes.setdefault("contacts", []).append(raw + " (name)")
        sentence_start = False
    return {cls: sorted(set(v)) for cls, v in sorted(classes.items())}

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

    def _class_scoped(self, task_id: str, classes: list[str]) -> dict:
        rules = [r for r in self._rules if r.task_id in ("*", task_id)
                 and (r.resource == "*" or resource_class(r.resource) in classes)]
        projections = {n: p for n, p in PROJECTION_SHAPES.items()
                       if resource_class(p["resource_shape"]) in classes}
        effects = {n: e for n, e in EFFECT_SHAPES.items()
                   if resource_class(e["grant_resource"]) in classes}
        return {"rules": [_entry(r) for r in rules], "projections": projections,
                "effects": effects}

    def denial_view(self, task_id: str, resource: str, operation: str,
                    reason: str) -> dict:
        """Denial-triggered hint: the policy's shape for the denied resource
        class only. Grants nothing; names no object; answers "what narrower
        or alternative authority can be requested?" for that class."""
        cls = resource_class(resource or "")
        body = {
            "schema": HINT_SCHEMA,
            "policy_version": self.policy_version,
            "task_id": task_id,
            "trigger": "denial",
            # the refusal/denial reason is already in the result's error
            # field; it is deliberately not repeated in the hint so the hint
            # itself stays policy-only (see FINDINGS_PHASE5 open defect on
            # refusal-reason oracles)
            "denied": {"resource": resource, "operation": operation},
            "scope": [cls],
            "default_decision": "deny",
            **self._class_scoped(task_id, [cls]),
            "defaults": self._defaults,
            "note": "This hint grants no authority and shows no content. "
                    "Request a scope/projection a listed approve rule covers.",
        }
        body["catalog_hash"] = canonical_hash(body)
        return body

    def task_view(self, task_id: str, prompt: str) -> dict:
        """Task-conditioned view: only the classes the prompt is grounded
        to. Task-specific rules are always included (they are about this
        task). Grants nothing; names no object."""
        grounding = ground_prompt(prompt)
        classes = sorted(grounding) or []
        scoped = self._class_scoped(task_id, classes)
        own = [_entry(r) for r in self._rules if r.task_id == task_id
               and _entry(r) not in scoped["rules"]]
        body = {
            "schema": VIEW_SCHEMA,
            "policy_version": self.policy_version,
            "task_id": task_id,
            "trigger": "initial",
            "scope": classes,
            "grounding": grounding,
            "default_decision": "deny",
            "rules": scoped["rules"] + own,
            "projections": scoped["projections"],
            "effects": scoped["effects"],
            "defaults": self._defaults,
            "note": "This view grants no authority and shows no content. "
                    "Rules are consulted in order; the first match decides; "
                    "no match means deny.",
        }
        body["catalog_hash"] = canonical_hash(body)
        return body
