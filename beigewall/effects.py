"""Effect handlers: the only way the agent changes the world.

Effects are trusted code, invoked by the broker after grant validation. Each
returns a small summary dict for the effect receipt.
"""

from .corpus import Corpus


class EffectError(Exception):
    pass


def _send_message(corpus: Corpus, params: dict) -> dict:
    thread = corpus.get(params["thread_id"])
    if thread.get("type") != "thread":
        raise EffectError(f"{params['thread_id']} is not a thread")
    outbox_id = f"out-{len(corpus.outbox) + 1}"
    corpus.outbox.append({"id": outbox_id, "type": "outbound_message",
                          "thread_id": params["thread_id"], "body": params["body"]})
    return {"outbox_id": outbox_id, "thread_id": params["thread_id"]}


def _create_event(corpus: Corpus, params: dict) -> dict:
    n = len(corpus.of_type("event")) + 1
    event_id = f"evt-new-{n}"
    corpus.add({"id": event_id, "type": "event", "title": params["title"],
                "start": params["start"], "attendees": params.get("attendees", []),
                "linked_thread": params.get("linked_thread")})
    return {"event_id": event_id}


def _update_note(corpus: Corpus, params: dict) -> dict:
    note = corpus.get(params["note_id"])
    if note.get("type") != "note":
        raise EffectError(f"{params['note_id']} is not a note")
    note["body"] = params["body"]
    return {"note_id": params["note_id"]}


EFFECTS = {
    "send_message": _send_message,
    "create_event": _create_event,
    "update_note": _update_note,
}
