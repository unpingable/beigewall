# Capability discovery (phase 4)

Capability discovery answers "what can I legitimately ask BLACKWALL for?"
without answering "what is in the protected corpus?". It is **not**
authorization: it grants nothing, discloses no corpus value, consults no
grant state, and cannot confirm whether a particular resource id exists.

## Surface

- **Broker**: `CapabilityBroker.discover(task_id, resource=None)` returns
  `{"ok": True, "receipt_id", "catalog"}` and appends a `discovery` receipt.
- **Facade**: `ToolFacade.discover(resource=None)` exists **only** when the
  harness is constructed with `discovery=True` (treatment). In the control
  condition the attribute does not exist, so a call to `discover` is trapped
  and receipted as an `unmediated_attempt` like any other unknown name.
- **LLM driver**: `LLMDriver(..., discovery=True)` adds one tool (`discover`)
  to the tool schema and one bullet to the system prompt (the
  `DISCOVERY_PROMPT_ADDENDUM` in `blackwall/agent/drivers.py`). Nothing else
  about the prompt, tools, task wording, or policy differs between
  conditions.
- **Runner**: `run_llm_experiment.py --condition {control,treatment}`;
  `meta.json` records `condition`, `capability_discovery`, and the
  `policy_version` hash.

## Catalog schema (`capability-catalog@1`)

```
{
  "schema": "capability-catalog@1",
  "policy_version": sha256(canonical JSON of the policy rule table),
  "task_id": "...",
  "query": null | "<resource pattern or name>",
  "query_matched": null | bool,          # does any rule's scope cover the query
  "default_decision": "deny",
  "rules": [ {resource, match: "pattern"|"exact", operation, projection,
              decision: "approve"|"deny", reason?, budget_cap?, ttl_cap?} ],
                                          # rules applicable to this task, in
                                          # policy order (first match wins)
  "defaults": {"ttl": 600, "max_ttl": 900, "budget": {"reads": 8, "objects": 200}},
  "projections": { name@version: {resource_shape, fields[]} },
  "effects": { name: {grant_resource, params[]} },
  "request_schema": { request_grant, read, effect },
  "note": "...",
  "catalog_hash": sha256(canonical JSON of everything above)
}
```

## What is exposed

- The policy rules that apply to the current task (task-specific rules plus
  wildcard rules), including deny rules and their reasons, budget and TTL
  caps, and whether a rule matches literally or as a pattern.
- Resource *patterns* and the literal scopes the policy itself names
  (`messages/*`, `notes/*`, `contacts/*`, `calendar/events`,
  `messages/thread/thr-alice` for the send rule, `messages/thread/thr-group`
  for the t6 budget cap). These ids are already in every driver's addressing
  manifest.
- Projection names, versions, the resource shape each handles, and the field
  *paths* each discloses (schema vocabulary, asserted against the live
  registry in tests).
- Effect names, the resource each must be granted on, and parameter names.
- Grant defaults and the request schema.

## What remains hidden

- Every corpus value: message bodies, titles, names, emails, attendees,
  attachment names, note text, timestamps. Asserted in
  `tests/test_capabilities.py` against every value in the demo corpus.
- Object ids that only disclosure reveals (`att-1`, `ct-*`, `evt-*`,
  `msg-*`).
- Whether a queried resource exists: `discover(resource="messages/thread/
  thr-nobody")` returns the same read rules as for `thr-alice` because
  patterns match names, not objects. Only the resource *class* can be
  unknown (`query_matched: false`).
- Grant state: discovery never reads or writes grants; the authority's
  decision counters do not move.

## Receipt

`{"kind": "discovery", "task_id", "schema", "policy_version", "catalog_hash",
"query", "query_matched", "rules_shown"}` — no `projection`, no `content`.
The receipt-grounded evaluator's disclosure set ignores it; metrics count it
separately from broker calls (`discovery_calls`), never as a read.

## Abuse telemetry (`blackwall/negotiation.py`)

Per task, from receipts and the pure policy matcher: discovery calls, calls
before the first grant request, calls after a denial, distinct and repeated
queries, unmatched (unknown-class) queries; grant requests classified as
obtainable / unavailable / overbroad / unknown-class / repeated; breadth
escalations (a later request strictly broader than an earlier one);
successful narrowing after a denied overbroad request.
