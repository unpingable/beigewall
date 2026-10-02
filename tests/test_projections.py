import json
import unittest

from beigewall.corpus import build_demo_corpus
from beigewall.projections import ProjectionError, run_projection


class TestConversationProjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = build_demo_corpus()

    def test_discloses_bodies_and_senders_but_no_emails(self):
        r = run_projection("conversation@1", self.c, "messages/thread/thr-alice")
        blob = json.dumps(r.content)
        self.assertIn("quarterly plan", blob)
        self.assertIn("ct-alice", blob)
        self.assertNotIn("alice@work.example", blob)
        self.assertNotIn("alice@home.example", blob)
        self.assertNotIn("sender_email", blob)

    def test_result_carries_provenance_and_accounting(self):
        r = run_projection("conversation@1", self.c, "messages/thread/thr-alice")
        self.assertEqual(r.projection, "conversation@1")
        self.assertIn("msg-1", r.source_object_ids)
        self.assertIn("thr-alice", r.source_object_ids)
        self.assertIn("message.body", r.disclosed_fields)
        self.assertEqual(r.accounting["objects_disclosed"], 4)  # thread + 3 messages
        self.assertGreater(r.accounting["bytes_disclosed"], 0)
        self.assertEqual(len(r.content_hash), 64)

    def test_v2_adds_sender_email_and_changes_hash(self):
        r1 = run_projection("conversation@1", self.c, "messages/thread/thr-alice")
        r2 = run_projection("conversation@2", self.c, "messages/thread/thr-alice")
        self.assertIn("alice@work.example", json.dumps(r2.content))
        self.assertNotEqual(r1.content_hash, r2.content_hash)
        self.assertIn("message.sender_email", r2.disclosed_fields)

    def test_quoted_reply_and_attachment_ids_disclosed(self):
        r = run_projection("conversation@1", self.c, "messages/thread/thr-alice")
        msgs = {m["id"]: m for m in r.content["messages"]}
        self.assertEqual(msgs["msg-2"]["quoted_message_id"], "msg-1")
        self.assertEqual(msgs["msg-3"]["attachment_ids"], ["att-1"])


class TestOtherProjections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = build_demo_corpus()

    def test_contact_discloses_emails(self):
        r = run_projection("contact@1", self.c, "contacts/ct-alice")
        self.assertIn("alice@home.example", json.dumps(r.content))

    def test_directory_discloses_no_emails(self):
        r = run_projection("contact-directory@1", self.c, "contacts/*")
        blob = json.dumps(r.content)
        self.assertIn("Alice Ander", blob)
        self.assertNotIn("@work.example", blob)
        self.assertNotIn("emails", blob)

    def test_note_discloses_links(self):
        r = run_projection("note@1", self.c, "notes/note-1")
        self.assertIn("messages/thread/thr-alice", json.dumps(r.content))

    def test_agenda_discloses_linked_thread(self):
        r = run_projection("agenda@1", self.c, "calendar/events")
        blob = json.dumps(r.content)
        self.assertIn("thr-alice", blob)
        self.assertIn("priya@example.com", blob)

    def test_attachment_discloses_extracted_text(self):
        r = run_projection("attachment@1", self.c, "messages/attachment/att-1")
        self.assertIn("hiring freeze", json.dumps(r.content))
        self.assertEqual(r.source_object_ids, ["att-1"])

    def test_unknown_projection_raises(self):
        with self.assertRaises(ProjectionError):
            run_projection("conversation@9", self.c, "messages/thread/thr-alice")

    def test_wrong_resource_shape_raises(self):
        with self.assertRaises(ProjectionError):
            run_projection("conversation@1", self.c, "contacts/ct-alice")

    def test_hash_stable_for_same_input(self):
        a = run_projection("note@1", self.c, "notes/note-1")
        b = run_projection("note@1", self.c, "notes/note-1")
        self.assertEqual(a.content_hash, b.content_hash)


if __name__ == "__main__":
    unittest.main()
