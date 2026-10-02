"""Protected store: the synthetic desktop corpus.

Only trusted code (projections, effect handlers) may hold a Corpus reference.
The agent never does; its only surface is the broker. In this spike the
boundary is enforced by construction (no reference is handed out), not by OS
isolation — see README "Known limitations".
"""


class Corpus:
    def __init__(self):
        self._objects: dict[str, dict] = {}
        self.outbox: list[dict] = []  # effect side: send_message appends here

    def add(self, obj: dict) -> None:
        self._objects[obj["id"]] = obj

    def get(self, object_id: str) -> dict:
        return self._objects[object_id]

    def of_type(self, type_name: str) -> list[dict]:
        return [o for o in self._objects.values() if o.get("type") == type_name]

    def find_contact_by_email(self, email: str) -> dict | None:
        for c in self.of_type("contact"):
            if email in c.get("emails", []):
                return c
        return None


def build_demo_corpus() -> Corpus:
    """Seeded fixtures with intentional tricky cases (see docs/PLAN.md)."""
    c = Corpus()

    c.add({"id": "ct-self", "type": "contact", "name": "You",
           "emails": ["you@desktop.example"], "tags": ["self"]})
    c.add({"id": "ct-alice", "type": "contact", "name": "Alice Ander",
           "emails": ["alice@work.example", "alice@home.example"],
           "tags": ["team"]})
    c.add({"id": "ct-bob", "type": "contact", "name": "Robert Beale",
           "emails": ["bob@work.example"], "tags": ["team"]})
    c.add({"id": "ct-carol", "type": "contact", "name": "Carol Corte",
           "emails": ["carol@work.example"], "tags": ["team"]})

    c.add({"id": "thr-alice", "type": "thread", "title": "Quarterly plan",
           "participant_ids": ["ct-alice", "ct-self"]})
    c.add({"id": "thr-group", "type": "thread", "title": "Budget numbers",
           "participant_ids": ["ct-alice", "ct-bob", "ct-carol", "ct-self"]})

    c.add({"id": "msg-1", "type": "message", "thread_id": "thr-alice",
           "sender_id": "ct-alice", "sender_label": "Alice Ander",
           "ts": "2026-09-28T09:12:00",
           "body": "Can you review the quarterly plan before Friday?",
           "quoted_message_id": None, "attachment_ids": []})
    c.add({"id": "msg-2", "type": "message", "thread_id": "thr-alice",
           "sender_id": "ct-self", "sender_label": "You",
           "ts": "2026-09-28T09:20:00",
           "body": "Sure — send the draft and I will look today.",
           "quoted_message_id": "msg-1", "attachment_ids": []})
    c.add({"id": "msg-3", "type": "message", "thread_id": "thr-alice",
           "sender_id": "ct-alice", "sender_label": "Alice Ander",
           "ts": "2026-09-28T09:31:00",
           "body": "Draft attached. The hiring section is still rough.",
           "quoted_message_id": "msg-2", "attachment_ids": ["att-1"]})
    c.add({"id": "att-1", "type": "attachment", "message_id": "msg-3",
           "name": "quarterly-plan.pdf", "mime": "application/pdf", "size": 48211,
           "extracted_text": "Quarterly plan draft: revenue targets flat, "
                             "hiring freeze except two senior roles, offsite in November."})

    group_bodies = [
        ("ct-alice", "Alice Ander", "Budget numbers for Q4 are in the shared sheet."),
        ("ct-bob", "Bobby", "The travel line looks high to me."),
        ("ct-carol", "Carol Corte", "Agreed — can we trim the offsite?"),
        ("ct-alice", "Alice Ander", "Offsite is already committed; look at contractors."),
        ("ct-bob", "Bobby", "Contractor spend is mostly the migration project."),
        ("ct-self", "You", "I will pull the contractor breakdown tomorrow."),
        ("ct-carol", "Carol Corte", "Also the tooling renewals land in Q4."),
        ("ct-bob", "Bobby", "Right, two renewals we can downgrade."),
        ("ct-alice", "Alice Ander", "Please put proposals in the thread by Thursday."),
        ("ct-carol", "Carol Corte", "Will do. I will take tooling."),
        ("ct-bob", "Bobby", "I will take travel scenarios."),
        ("ct-self", "You", "And I will reconcile against last quarter."),
    ]
    for i, (sender, label, body) in enumerate(group_bodies):
        quoted = None
        if i == 2:
            quoted = "msg-10"  # carol quotes alice's opener
        if i == 4:
            quoted = "msg-13"  # bob quotes his own earlier line
        c.add({"id": f"msg-{10 + i}", "type": "message", "thread_id": "thr-group",
               "sender_id": sender, "sender_label": label,
               "ts": f"2026-09-29T10:{i:02d}:00",
               "body": body, "quoted_message_id": quoted, "attachment_ids": []})

    c.add({"id": "note-1", "type": "note", "title": "Follow-ups",
           "body": "Review the draft Alice sent. Schedule coffee with Priya "
                   "about the offsite.",
           "links": ["messages/thread/thr-alice"]})

    c.add({"id": "evt-1", "type": "event", "title": "Quarterly plan review",
           "start": "2026-10-05T14:00:00",
           "attendees": ["ct-alice", "ct-self"], "linked_thread": "thr-alice"})
    c.add({"id": "evt-2", "type": "event", "title": "Offsite planning coffee",
           "start": "2026-10-07T09:30:00",
           "attendees": ["priya@example.com", "ct-self"], "linked_thread": None})

    return c
