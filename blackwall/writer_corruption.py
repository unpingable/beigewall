"""A deterministic, controlled *compromised ledger writer* (phase 7B).

Each function takes the canonical receipt rows (as the trusted producer minted
them) and returns the rows the compromised writer actually persisted to the
primary ledger. Mutations are deterministic so the evidence result is
reproducible. This models the writer's authority ONLY over persistence — it
never mints grants, authorizes effects or changes trusted state; it only lies
about what it stored. The witness/observer must detect every mutation.

Names match the phase-7B attack matrix in FINDINGS_PHASE7B.md.
"""

import copy


def _clone(rows):
    return [copy.deepcopy(r) for r in rows]


def identity(rows):
    """Honest writer: persist exactly what was produced."""
    return _clone(rows)


def omit_event(rows, index=None):
    rows = _clone(rows)
    i = len(rows) // 2 if index is None else index
    del rows[i]
    return rows


def selective_omission(rows, kind="read"):
    """Drop every row of one kind (e.g. hide all disclosures)."""
    return [r for r in _clone(rows) if r.get("kind") != kind]


def altered_field(rows, index=None):
    rows = _clone(rows)
    i = len(rows) // 2 if index is None else index
    rows[i] = dict(rows[i])
    rows[i]["_tamper"] = "injected"
    return rows


def altered_source_object(rows):
    """Change a read's disclosed object set."""
    rows = _clone(rows)
    for r in rows:
        if r.get("kind") == "read" and "objects" in r:
            r["objects"] = (r["objects"] or [])[:-1] or ["swapped"]
            return rows
        if r.get("kind") == "read":
            r["resource"] = "contacts/evil"
            return rows
    return altered_field(rows)


def altered_provenance(rows):
    rows = _clone(rows)
    for r in rows:
        if r.get("kind") == "effect" and "derived_from" in r:
            r["derived_from"] = [{"type": "forged", "ref": "x"}]
            return rows
        if r.get("kind") == "effect":
            r["derived_from"] = [{"type": "forged", "ref": "x"}]
            return rows
    return altered_field(rows)


def _alter_first(rows, kind, field, value):
    rows = _clone(rows)
    for r in rows:
        if r.get("kind") == kind:
            r[field] = value
            return rows
    # fall back: alter any row's field so the attack still mutates content
    rows[0][field] = value
    return rows


def altered_task_identity(rows):
    return _alter_first(rows, "task_end", "task_id", "t-impostor")


def altered_grant_identity(rows):
    return _alter_first(rows, "grant_mint", "grant_id", "g-forged")


def altered_effect_identity(rows):
    return _alter_first(rows, "effect", "effect_id", "e-forged")


def duplicated_event(rows, index=None):
    rows = _clone(rows)
    i = len(rows) // 2 if index is None else index
    rows.insert(i + 1, copy.deepcopy(rows[i]))
    return rows


def replayed_event(rows, index=2):
    """Re-present an early event at the tail (out of its original position)."""
    rows = _clone(rows)
    rows.append(copy.deepcopy(rows[index]))
    return rows


def reordered_events(rows, i=None, j=None):
    rows = _clone(rows)
    i = len(rows) // 3 if i is None else i
    j = i + 1 if j is None else j
    rows[i], rows[j] = rows[j], rows[i]
    return rows


def truncated_ledger(rows, keep=None):
    rows = _clone(rows)
    keep = max(1, len(rows) - 3) if keep is None else keep
    return rows[:keep]


def rollback(rows, keep=None):
    """Revert to an earlier state of the same ledger (a restored backup)."""
    keep = len(rows) // 2 if keep is None else keep
    return _clone(rows[:keep])


def fabricated_event(rows):
    rows = _clone(rows)
    fake = {"receipt_id": "r-9999", "seq": 9999, "ts": 1.0,
            "kind": "effect", "task_id": "t1", "effect": "send_message",
            "params": {"thread_id": "evil"}, "grant_id": "g-1",
            "derived_from": []}
    rows.append(fake)
    return rows


def forked_history(rows, at=None):
    """Produce a second snapshot that shares a prefix then diverges — the
    writer equivocates. Returns (snapshot_a, snapshot_b)."""
    at = len(rows) // 2 if at is None else at
    a = _clone(rows)
    b = _clone(rows[:at])
    tail = copy.deepcopy(rows[at])
    tail["_branch"] = "B"
    b.append(tail)
    for r in rows[at + 1:]:
        b.append(copy.deepcopy(r))
    return a, b


def corrupted_budget_record(rows):
    """Lie about effect-budget consumption by removing a consumed effect."""
    rows = _clone(rows)
    for i, r in enumerate(rows):
        if r.get("kind") == "effect":
            del rows[i]
            return rows
    return omit_event(rows)


def corrupted_lifecycle_record(rows):
    return _alter_first(rows, "trusted_restart", "grant_seq_resumed", 0)


def writer_crash_midway(rows):
    """Persistence stopped partway: a truncation plus a torn last line is the
    writer's business; here we model the committed effect of a crash as the
    rows that reached disk (a prefix)."""
    return truncated_ledger(rows, keep=max(1, len(rows) - 1))


# attack name -> callable(rows) -> primary_rows  (fork returns a tuple)
SINGLE_SNAPSHOT = {
    "omitted_event": omit_event,
    "altered_field": altered_field,
    "altered_source_object": altered_source_object,
    "altered_provenance": altered_provenance,
    "altered_task_identity": altered_task_identity,
    "altered_grant_identity": altered_grant_identity,
    "altered_effect_identity": altered_effect_identity,
    "duplicated_event": duplicated_event,
    "replayed_event": replayed_event,
    "reordered_events": reordered_events,
    "truncated_ledger": truncated_ledger,
    "rollback": rollback,
    "fabricated_event": fabricated_event,
    "selective_omission": selective_omission,
    "corrupted_budget_record": corrupted_budget_record,
    "corrupted_lifecycle_record": corrupted_lifecycle_record,
    "writer_crash_midway": writer_crash_midway,
}
