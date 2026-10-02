"""Projection engine: the primary disclosure boundary.

A projection is trusted code that explicitly defines what leaves the protected
store. Every result carries its schema@version, the source object identifiers,
the disclosed field paths, a content hash, and disclosure accounting — so a
receipt can say "projection conversation@1 disclosed message.body from msg-1,
msg-2, msg-3" rather than "read thread alice".
"""

import json
from dataclasses import dataclass

from .corpus import Corpus
from .receipts import canonical_hash


class ProjectionError(Exception):
    """Unknown projection, or resource shape not handled by it."""


@dataclass(frozen=True)
class ProjectionResult:
    projection: str
    source_object_ids: list[str]
    disclosed_fields: list[str]
    content: dict
    content_hash: str
    accounting: dict


def _result(name: str, source_ids: list[str], fields: list[str], content: dict) -> ProjectionResult:
    blob = json.dumps(content, sort_keys=True)
    return ProjectionResult(
        projection=name,
        source_object_ids=source_ids,
        disclosed_fields=fields,
        content=content,
        content_hash=canonical_hash(content),
        accounting={
            "objects_examined": len(source_ids),
            "objects_disclosed": len(source_ids),
            "fields_disclosed": len(fields),
            "bytes_disclosed": len(blob.encode("utf-8")),
        },
    )


def _thread_messages(corpus: Corpus, thread_id: str) -> list[dict]:
    thread = corpus.get(thread_id)
    if thread.get("type") != "thread":
        raise ProjectionError(f"{thread_id} is not a thread")
    msgs = [m for m in corpus.of_type("message") if m.get("thread_id") == thread_id]
    return [thread] + sorted(msgs, key=lambda m: m["ts"])


def _conversation_v1(corpus: Corpus, resource: str) -> ProjectionResult:
    tid = _expect(resource, "messages/thread/")
    objs = _thread_messages(corpus, tid)
    thread, msgs = objs[0], objs[1:]
    content = {
        "thread_id": tid,
        "title": thread["title"],
        "messages": [
            {
                "id": m["id"],
                "sender_id": m["sender_id"],
                "sender_label": m["sender_label"],
                "ts": m["ts"],
                "body": m["body"],
                "quoted_message_id": m["quoted_message_id"],
                "attachment_ids": m["attachment_ids"],
            }
            for m in msgs
        ],
    }
    fields = ["thread.title", "message.id", "message.sender_id", "message.sender_label",
              "message.ts", "message.body", "message.quoted_message_id",
              "message.attachment_ids"]
    return _result("conversation@1", [o["id"] for o in objs], fields, content)


def _conversation_v2(corpus: Corpus, resource: str) -> ProjectionResult:
    tid = _expect(resource, "messages/thread/")
    objs = _thread_messages(corpus, tid)
    thread, msgs = objs[0], objs[1:]
    messages = []
    for m in msgs:
        contact = corpus.get(m["sender_id"])
        messages.append({
            "id": m["id"],
            "sender_id": m["sender_id"],
            "sender_label": m["sender_label"],
            "sender_email": contact["emails"][0],
            "ts": m["ts"],
            "body": m["body"],
            "quoted_message_id": m["quoted_message_id"],
            "attachment_ids": m["attachment_ids"],
        })
    fields = ["thread.title", "message.id", "message.sender_id", "message.sender_label",
              "message.sender_email", "message.ts", "message.body",
              "message.quoted_message_id", "message.attachment_ids"]
    return _result("conversation@2", [o["id"] for o in objs], fields,
                   {"thread_id": tid, "title": thread["title"], "messages": messages})


def _contact_v1(corpus: Corpus, resource: str) -> ProjectionResult:
    cid = _expect(resource, "contacts/")
    contact = corpus.get(cid)
    if contact.get("type") != "contact":
        raise ProjectionError(f"{cid} is not a contact")
    content = {"id": cid, "name": contact["name"], "emails": contact["emails"],
               "tags": contact.get("tags", [])}
    return _result("contact@1", [cid],
                   ["contact.name", "contact.emails", "contact.tags"], content)


def _directory_v1(corpus: Corpus, resource: str) -> ProjectionResult:
    if resource not in ("contacts", "contacts/*"):
        raise ProjectionError(f"contact-directory@1 does not handle {resource}")
    contacts = corpus.of_type("contact")
    content = {"contacts": [{"id": c["id"], "name": c["name"]} for c in contacts]}
    return _result("contact-directory@1", [c["id"] for c in contacts],
                   ["contact.id", "contact.name"], content)


def _note_v1(corpus: Corpus, resource: str) -> ProjectionResult:
    nid = _expect(resource, "notes/")
    note = corpus.get(nid)
    if note.get("type") != "note":
        raise ProjectionError(f"{nid} is not a note")
    content = {"id": nid, "title": note["title"], "body": note["body"],
               "links": note.get("links", [])}
    return _result("note@1", [nid],
                   ["note.title", "note.body", "note.links"], content)


def _agenda_v1(corpus: Corpus, resource: str) -> ProjectionResult:
    if resource != "calendar/events":
        raise ProjectionError(f"agenda@1 does not handle {resource}")
    events = corpus.of_type("event")
    content = {"events": [
        {"id": e["id"], "title": e["title"], "start": e["start"],
         "attendees": e["attendees"], "linked_thread": e.get("linked_thread")}
        for e in events
    ]}
    return _result("agenda@1", [e["id"] for e in events],
                   ["event.title", "event.start", "event.attendees",
                    "event.linked_thread"], content)


def _attachment_v1(corpus: Corpus, resource: str) -> ProjectionResult:
    aid = _expect(resource, "messages/attachment/")
    att = corpus.get(aid)
    if att.get("type") != "attachment":
        raise ProjectionError(f"{aid} is not an attachment")
    content = {"id": aid, "name": att["name"], "mime": att["mime"],
               "size": att["size"], "extracted_text": att["extracted_text"]}
    return _result("attachment@1", [aid],
                   ["attachment.name", "attachment.mime", "attachment.size",
                    "attachment.extracted_text"], content)


def _expect(resource: str, prefix: str) -> str:
    if not resource.startswith(prefix) or len(resource) <= len(prefix):
        raise ProjectionError(f"resource {resource!r} does not match {prefix}<id>")
    return resource[len(prefix):]


_REGISTRY = {
    "conversation@1": _conversation_v1,
    "conversation@2": _conversation_v2,
    "contact@1": _contact_v1,
    "contact-directory@1": _directory_v1,
    "note@1": _note_v1,
    "agenda@1": _agenda_v1,
    "attachment@1": _attachment_v1,
}


def run_projection(projection: str, corpus: Corpus, resource: str) -> ProjectionResult:
    fn = _REGISTRY.get(projection)
    if fn is None:
        raise ProjectionError(f"unknown projection {projection!r}")
    return fn(corpus, resource)
