import unittest

from blackwall.grants import Budget
from blackwall.authority import PolicyAuthority, PolicyRule


def req(authority, **kw):
    defaults = dict(task_id="t1", resource="messages/thread/thr-alice",
                    operation="read", projection="conversation@1",
                    ttl=600.0, budget=Budget(reads=8, objects=200))
    defaults.update(kw)
    return authority.decide(**defaults)


class TestPolicyAuthority(unittest.TestCase):
    def test_approve_mints_grant(self):
        a = PolicyAuthority(
            rules=[PolicyRule(task_id="*", resource="messages/*", operation="read",
                              projection="*", decision="approve")],
            clock=lambda: 100.0)
        grant, reason = req(a)
        self.assertIsNotNone(grant)
        self.assertEqual(grant.grant_id, "g-1")
        self.assertEqual(grant.expires_at, 100.0 + 600.0)
        self.assertEqual(grant.budget.reads, 8)

    def test_default_deny_fail_closed(self):
        a = PolicyAuthority(rules=[], clock=lambda: 0.0)
        grant, reason = req(a)
        self.assertIsNone(grant)
        self.assertIn("no policy rule", reason)

    def test_explicit_deny_with_reason(self):
        a = PolicyAuthority(
            rules=[PolicyRule(task_id="t5", resource="contacts/ct-alice",
                              operation="read", projection="contact@1",
                              decision="deny", reason="personal data not needed")],
            clock=lambda: 0.0)
        grant, reason = req(a, task_id="t5", resource="contacts/ct-alice",
                            projection="contact@1")
        self.assertIsNone(grant)
        self.assertEqual(reason, "personal data not needed")

    def test_first_matching_rule_wins(self):
        a = PolicyAuthority(
            rules=[
                PolicyRule(task_id="t5", resource="contacts/*", operation="*",
                           projection="*", decision="deny", reason="blocked"),
                PolicyRule(task_id="*", resource="contacts/*", operation="*",
                           projection="*", decision="approve"),
            ],
            clock=lambda: 0.0)
        grant, reason = req(a, task_id="t5", resource="contacts/ct-bob",
                            projection="contact@1")
        self.assertIsNone(grant)
        self.assertEqual(reason, "blocked")

    def test_approval_narrows_ttl_and_budget(self):
        a = PolicyAuthority(
            rules=[PolicyRule(task_id="*", resource="messages/*", operation="read",
                              projection="*", decision="approve",
                              ttl_cap=60.0, budget_cap=Budget(reads=2, objects=5))],
            clock=lambda: 0.0)
        grant, _ = req(a, ttl=600.0, budget=Budget(reads=8, objects=200))
        self.assertEqual(grant.expires_at, 60.0)
        self.assertEqual(grant.budget, Budget(reads=2, objects=5))

    def test_grant_ids_sequential(self):
        a = PolicyAuthority(
            rules=[PolicyRule(task_id="*", resource="*", operation="*",
                              projection="*", decision="approve")],
            clock=lambda: 0.0)
        g1, _ = req(a)
        g2, _ = req(a, resource="notes/note-1", projection="note@1")
        self.assertEqual((g1.grant_id, g2.grant_id), ("g-1", "g-2"))

    def test_every_request_is_an_authorization_event(self):
        a = PolicyAuthority(
            rules=[PolicyRule(task_id="*", resource="messages/*", operation="read",
                              projection="*", decision="approve")],
            clock=lambda: 0.0)
        req(a)                      # approve
        req(a, resource="notes/note-1")  # default deny
        self.assertEqual(a.authorization_events, 2)
        self.assertEqual(a.grants_minted, 1)
        self.assertEqual(a.grants_denied, 1)

    def test_effect_grant_has_no_projection(self):
        a = PolicyAuthority(
            rules=[PolicyRule(task_id="*", resource="messages/*",
                              operation="send_message", projection="*",
                              decision="approve")],
            clock=lambda: 0.0)
        grant, _ = req(a, operation="send_message", projection=None)
        self.assertIsNotNone(grant)
        self.assertIsNone(grant.projection)


if __name__ == "__main__":
    unittest.main()
