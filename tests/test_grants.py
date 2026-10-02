import unittest

from beigewall.grants import Budget, Grant, GrantError, scope_matches, validate_use


def make_grant(**kw):
    defaults = dict(
        grant_id="g-1",
        task_id="t1",
        resource="messages/thread/thr-alice",
        operation="read",
        projection="conversation@1",
        expires_at=1000.0,
        budget=Budget(reads=8, objects=200),
    )
    defaults.update(kw)
    return Grant(**defaults)


class TestScopeMatching(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(scope_matches("messages/thread/thr-alice", "messages/thread/thr-alice"))

    def test_exact_mismatch(self):
        self.assertFalse(scope_matches("messages/thread/thr-alice", "messages/thread/thr-group"))

    def test_wildcard_prefix_matches(self):
        self.assertTrue(scope_matches("contacts/*", "contacts/ct-alice"))

    def test_wildcard_does_not_match_sibling_tree(self):
        self.assertFalse(scope_matches("contacts/*", "messages/thread/thr-alice"))

    def test_wildcard_requires_descendant(self):
        self.assertFalse(scope_matches("contacts/*", "contacts"))

    def test_exact_does_not_cover_children(self):
        self.assertFalse(scope_matches("messages/thread/thr-alice", "messages/thread/thr-alice/extra"))


class TestValidateUse(unittest.TestCase):
    def test_valid_read_passes(self):
        g = make_grant()
        validate_use(g, operation="read", resource="messages/thread/thr-alice",
                     projection="conversation@1", now=500.0)

    def test_expired_grant_refused(self):
        g = make_grant()
        with self.assertRaises(GrantError):
            validate_use(g, operation="read", resource="messages/thread/thr-alice",
                         projection="conversation@1", now=1000.0)

    def test_operation_mismatch_refused(self):
        g = make_grant()
        with self.assertRaises(GrantError):
            validate_use(g, operation="send_message", resource="messages/thread/thr-alice",
                         projection=None, now=500.0)

    def test_resource_outside_scope_refused(self):
        g = make_grant()
        with self.assertRaises(GrantError):
            validate_use(g, operation="read", resource="messages/thread/thr-group",
                         projection="conversation@1", now=500.0)

    def test_projection_version_mismatch_refused(self):
        g = make_grant()
        with self.assertRaises(GrantError):
            validate_use(g, operation="read", resource="messages/thread/thr-alice",
                         projection="conversation@2", now=500.0)

    def test_read_grant_without_projection_refused_for_reads(self):
        g = make_grant(operation="send_message", projection=None)
        with self.assertRaises(GrantError):
            validate_use(g, operation="read", resource="messages/thread/thr-alice",
                         projection="conversation@1", now=500.0)

    def test_read_budget_exhaustion_refused(self):
        g = make_grant(budget=Budget(reads=1, objects=10))
        g.reads_used = 1
        with self.assertRaises(GrantError):
            validate_use(g, operation="read", resource="messages/thread/thr-alice",
                         projection="conversation@1", now=500.0)

    def test_object_budget_exhaustion_refused(self):
        g = make_grant(budget=Budget(reads=8, objects=3))
        g.objects_disclosed = 3
        with self.assertRaises(GrantError):
            validate_use(g, operation="read", resource="messages/thread/thr-alice",
                         projection="conversation@1", now=500.0)

    def test_effect_grant_validates_without_projection(self):
        g = make_grant(operation="send_message", projection=None)
        validate_use(g, operation="send_message", resource="messages/thread/thr-alice",
                     projection=None, now=500.0)


if __name__ == "__main__":
    unittest.main()
