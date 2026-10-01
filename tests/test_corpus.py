import unittest

from blackwall.corpus import Corpus, build_demo_corpus


class TestCorpusApi(unittest.TestCase):
    def test_get_and_of_type(self):
        c = Corpus()
        c.add({"id": "m-1", "type": "message", "body": "hi"})
        c.add({"id": "n-1", "type": "note", "body": "x"})
        self.assertEqual(c.get("m-1")["body"], "hi")
        self.assertEqual([o["id"] for o in c.of_type("note")], ["n-1"])
        with self.assertRaises(KeyError):
            c.get("missing")

    def test_find_contact_by_email(self):
        c = Corpus()
        c.add({"id": "ct-1", "type": "contact", "name": "A", "emails": ["a@x.example"]})
        self.assertEqual(c.find_contact_by_email("a@x.example")["id"], "ct-1")
        self.assertIsNone(c.find_contact_by_email("nobody@x.example"))


class TestDemoCorpusFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = build_demo_corpus()

    def test_threads_exist(self):
        self.assertIsNotNone(self.c.get("thr-alice"))
        self.assertIsNotNone(self.c.get("thr-group"))

    def test_quoted_reply_fixture(self):
        quoting = [m for m in self.c.of_type("message")
                   if m.get("thread_id") == "thr-alice" and m.get("quoted_message_id")]
        self.assertTrue(quoting, "thr-alice must contain a quoted reply")
        for m in quoting:
            self.assertIsNotNone(self.c.get(m["quoted_message_id"]))

    def test_attachment_backlink_consistent(self):
        att = self.c.get("att-1")
        self.assertEqual(att["type"], "attachment")
        msg = self.c.get(att["message_id"])
        self.assertIn("att-1", msg.get("attachment_ids", []))

    def test_group_thread_multi_participant_and_alias(self):
        msgs = [m for m in self.c.of_type("message") if m.get("thread_id") == "thr-group"]
        self.assertGreaterEqual(len(msgs), 10)
        senders = {m["sender_id"] for m in msgs}
        self.assertGreaterEqual(len(senders), 3)
        labels = {m["sender_id"]: m.get("sender_label") for m in msgs}
        bob = self.c.get("ct-bob")
        self.assertNotEqual(labels["ct-bob"], bob["name"])  # alias "Bobby"

    def test_note_crosslink_target_exists(self):
        note = self.c.get("note-1")
        self.assertIn("messages/thread/thr-alice", note["links"])
        self.assertIsNotNone(self.c.get("thr-alice"))

    def test_hidden_relationship_priya_has_no_contact(self):
        evt = self.c.get("evt-2")
        self.assertIn("priya@example.com", evt["attendees"])
        self.assertIsNone(self.c.find_contact_by_email("priya@example.com"))

    def test_calendar_event_links_thread(self):
        self.assertEqual(self.c.get("evt-1")["linked_thread"], "thr-alice")

    def test_alice_has_personal_email(self):
        alice = self.c.get("ct-alice")
        self.assertIn("alice@home.example", alice["emails"])


if __name__ == "__main__":
    unittest.main()
