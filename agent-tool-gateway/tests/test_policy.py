"""Regression checks for the policy logic moved from Personal_AI."""
import unittest
from datetime import datetime, timezone

from server.gateway.policy.engine import PolicyEngine
from server.gateway.policy.models import AccessRequest, PolicyRule


class PolicyTests(unittest.TestCase):
    def request(self):
        return AccessRequest("user:demo", "agent:demo", "read", "tool_execution",
                             "tool:read_note", "local", ("arguments",), ttl_seconds=30)

    def rule(self, effect="allow", **kwargs):
        values = dict(rule_id=effect, effect=effect, agent_ids=("agent:demo",),
                      purposes=("tool_execution",), resource_patterns=("tool:read_note",),
                      destinations=("local",), sensitivities=("private",),
                      allowed_fields=("arguments",), max_ttl_seconds=10)
        values.update(kwargs)
        return PolicyRule(**values)

    def test_default_deny(self):
        self.assertFalse(PolicyEngine([]).evaluate(self.request(), sensitivity="private").allowed)

    def test_explicit_deny_has_precedence(self):
        result = PolicyEngine([self.rule(), self.rule("deny")]).evaluate(self.request(), sensitivity="private")
        self.assertFalse(result.allowed)
        self.assertEqual("deny", result.rule_id)

    def test_lifetime_is_capped_and_bound_to_request(self):
        request = self.request()
        result = PolicyEngine([self.rule()]).evaluate(request, sensitivity="private",
                                                    now=datetime(2026, 10, 11, tzinfo=timezone.utc))
        self.assertTrue(result.allowed)
        self.assertEqual(request, result.request)
        self.assertEqual(10, (result.expires_at - result.issued_at).total_seconds())

    def test_ungranted_destination_and_fields_denied(self):
        for rule in (self.rule(destinations=("cloud",)), self.rule(allowed_fields=("key",))):
            self.assertFalse(PolicyEngine([rule]).evaluate(self.request(), sensitivity="private").allowed)
