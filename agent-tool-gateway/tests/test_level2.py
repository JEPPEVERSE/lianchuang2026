"""Level 2 acceptance: binding, concurrency, sandbox, and complete audit."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from test_gateway import GatewayFixture, HttpFixture
from server.__main__ import validate_users
from server.gateway.policy.engine import PolicyEngine
from server.gateway.service import GatewayService
from client.__main__ import run_call


class Level2Tests(GatewayFixture, unittest.TestCase):
    def test_saved_snapshot_is_immutable_and_detached(self):
        arguments = {"to": "mentor@example.com", "subject": "original", "body": "approved text"}
        pending = self.call("send_email", arguments)
        arguments["to"] = "attacker@example.com"
        pending["arguments"]["body"] = "changed response"
        record = self.gateway._approvals[pending["approval_id"]]
        with self.assertRaises(FrozenInstanceError):
            record.tool_name = "delete_note"
        detached = record.arguments
        detached["subject"] = "changed copy"
        self.decide(pending)
        stored = json.loads(next(self.outbox.iterdir()).read_text())
        self.assertEqual({"to": "mentor@example.com", "subject": "original", "body": "approved text"}, stored)

    def test_key_order_does_not_change_confirmation_binding(self):
        pending = self.call("send_email", {"to": "mentor@example.com", "subject": "a", "body": "b"})
        reordered = self.call("send_email", {"body": "b", "subject": "a", "to": "mentor@example.com"})
        self.assertEqual(pending["approval_id"], reordered["approval_id"])
        self.assertEqual("executed", self.decide(reordered)["status"])
        self.assertEqual(1, len(list(self.outbox.iterdir())))

    def test_expired_confirmation_becomes_terminal(self):
        pending = self.email()
        self.now += timedelta(seconds=300)
        self.assertEqual("deny", self.email()["decision"])
        self.now -= timedelta(seconds=300)
        self.assertEqual("deny", self.decide(pending)["decision"])
        self.assertEqual("denied", self.email()["status"])
        self.assertEqual([], list(self.outbox.iterdir()))

    def test_approval_can_execute_just_before_expiration(self):
        pending = self.email()
        self.now += timedelta(seconds=299, milliseconds=999)
        self.assertEqual("executed", self.decide(pending)["status"])

    def test_sandbox_failure_invalidates_confirmation(self):
        pending = self.call("delete_note", {"path": "draft.txt"})
        target = self.notes / "draft.txt"
        target.unlink()
        target.symlink_to(self.root / "outside.txt")
        self.assertEqual("deny", self.decide(pending)["decision"])
        target.unlink()
        target.write_text("restored")
        self.assertEqual("deny", self.decide(pending)["decision"])
        self.assertEqual("restored", target.read_text())
        self.assertEqual("denied", self.call("delete_note", {"path": "draft.txt"})["status"])

    def test_policy_is_checked_again_after_user_confirmation(self):
        pending = self.email()
        self.gateway.policy = PolicyEngine([])
        self.assertEqual("deny", self.decide(pending)["decision"])
        self.assertEqual([], list(self.outbox.iterdir()))

    def test_approval_id_cannot_be_rebound_to_another_call(self):
        first = self.email("first")
        second = self.email("second")
        record = self.gateway._approvals[first["approval_id"]]
        self.gateway._approvals[first["approval_id"]] = replace(record, call_id="second")
        self.assertEqual("deny", self.decide(first)["decision"])
        self.assertEqual("executed", self.decide(second)["status"])
        self.assertEqual(1, len(list(self.outbox.iterdir())))

    def test_sandbox_root_symlink_replacement_is_denied(self):
        sandbox = self.root / "sandbox"
        sandbox.rename(self.root / "original-sandbox")
        outside = self.root / "outside"
        (outside / "notes").mkdir(parents=True)
        (outside / "outbox").mkdir()
        (outside / "notes/todo.md").write_text("secret")
        sandbox.symlink_to(outside, target_is_directory=True)
        self.assertEqual("deny", self.call()["decision"])
        self.assertEqual("deny", self.call("delete_note", {"path": "todo.md"}, "delete")["decision"])
        self.assertEqual("secret", (outside / "notes/todo.md").read_text())
        pending = self.email()
        self.assertEqual("failed", self.decide(pending)["status"])
        self.assertEqual([], list((outside / "outbox").iterdir()))

    def test_approve_and_deny_race_has_one_terminal_outcome(self):
        pending = self.email()
        barrier = threading.Barrier(2)
        def decide(action):
            barrier.wait()
            return self.decide(pending, action)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(decide, ("approve", "deny")))
        record = self.gateway._approvals[pending["approval_id"]]
        self.assertNotEqual(record.used, record.rejected)
        executed = sum(result["status"] == "executed" for result in responses)
        self.assertEqual(int(record.used), executed)
        self.assertEqual(executed, len(list(self.outbox.iterdir())))
        self.assertEqual("deny", self.decide(pending)["decision"])

    def test_non_json_arguments_are_denied_and_audited(self):
        response = self.call("send_email", {"to": "mentor@example.com", "subject": "a", "body": float("nan")})
        self.assertEqual("deny", response["decision"])
        self.assertEqual("deny", self.events()[-1]["decision"])

    def test_invalid_ttl_rejected(self):
        for ttl in (0, -1, True, 1.5):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                GatewayService(self.root / "sandbox", self.root / "other.jsonl", approval_ttl=ttl)

    def test_audit_failure_before_execution_causes_no_side_effect(self):
        pending = self.email()
        original = self.gateway._audit
        def fail_before_execution(**kwargs):
            if kwargs.get("event") == "execution_started":
                raise OSError("audit unavailable")
            original(**kwargs)
        with patch.object(self.gateway, "_audit", side_effect=fail_before_execution):
            with self.assertRaises(OSError):
                self.decide(pending)
        self.assertEqual([], list(self.outbox.iterdir()))
        self.assertEqual("deny", self.decide(pending)["decision"])
        self.assertEqual("unknown", self.email()["status"])

    def test_rejection_and_repeat_are_audited_with_user_action(self):
        pending = self.email()
        self.decide(pending, "deny")
        self.decide(pending, "approve")
        entries = [entry for entry in self.events() if entry["approval_id"] == pending["approval_id"]]
        self.assertEqual([None, "deny", "approve"], [entry["user_action"] for entry in entries])
        self.assertFalse(any(entry["event"] == "execution_started" for entry in entries))


class Level2HttpTests(HttpFixture, unittest.TestCase):
    def submit_email(self, call_id="http-mail"):
        return self.raw_post("/tool-calls", json.dumps({"call_id": call_id, "tool_name": "send_email",
            "arguments": {"to": "mentor@example.com", "subject": "a", "body": "b"}}))

    def test_concurrent_http_approvals_execute_once(self):
        pending = self.submit_email()
        barrier = threading.Barrier(8)
        def approve(_):
            barrier.wait()
            return self.raw_post(f"/approvals/{pending['approval_id']}/decision", '{"action":"approve"}')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(approve, range(8)))
        self.assertEqual(1, sum(result["status"] == "executed" for result in results))
        self.assertEqual(7, sum(result["status"] == "denied" for result in results))
        self.assertEqual(1, len(list(self.outbox.iterdir())))
        self.assertEqual(1, sum(entry["event"] == "execution_finished" for entry in self.events()))

    def test_wrong_identity_and_modified_parameters_cannot_execute(self):
        pending = self.submit_email()
        endpoint = f"/approvals/{pending['approval_id']}/decision"
        for body, token in [('{"action":"approve"}', "other-token"),
                            ('{"action":"approve","user_id":"user:demo"}', "other-token"),
                            ('{"action":"approve","tool_name":"delete_note"}', "test-token"),
                            ('{"action":"approve","arguments":{"to":"attacker@example.com"}}', "test-token")]:
            self.assertEqual("deny", self.raw_post(endpoint, body, token)["decision"])
        self.assertEqual([], list(self.outbox.iterdir()))
        self.assertEqual("executed", self.raw_post(endpoint, '{"action":"approve"}')["status"])

    def test_expiration_is_enforced_over_http(self):
        pending = self.submit_email()
        self.now += timedelta(seconds=300)
        result = self.raw_post(f"/approvals/{pending['approval_id']}/decision", '{"action":"approve"}')
        self.assertEqual("deny", result["decision"])
        self.assertEqual([], list(self.outbox.iterdir()))
        self.assertEqual("denied", self.submit_email()["status"])

    def test_transport_rejections_are_audited_without_credentials(self):
        for endpoint, body, token, status in [
            ("/tool-calls", "{}", "bad-token", 401),
            ("/tool-calls", "{invalid", "test-token", 400),
            ("/tool-calls", '{"action":"approve","action":"deny"}', "test-token", 400),
            ("/missing", "{}", "test-token", 404),
        ]:
            with self.assertRaises(HTTPError) as exc:
                self.raw_post(endpoint, body, token)
            self.assertEqual(status, exc.exception.code)
        entries = self.events()
        self.assertEqual(4, len(entries))
        self.assertEqual("unauthenticated", entries[0]["user_id"])
        self.assertTrue(all(entry["decision"] == "deny" for entry in entries))
        log = (self.root / "audit.jsonl").read_text()
        self.assertNotIn("test-token", log)
        self.assertNotIn("bad-token", log)

    def test_unicode_invalid_credential_is_rejected(self):
        with self.assertRaises(HTTPError) as exc:
            self.raw_post("/tool-calls", "{}", "é")
        self.assertEqual(401, exc.exception.code)

    def test_client_displays_full_parameters_and_denies_by_default(self):
        payload = {"call_id": "client-deny", "tool_name": "send_email",
                   "arguments": {"to": "mentor@example.com", "subject": "题目", "body": "完整正文"}}
        with patch("builtins.input", return_value=""), patch("builtins.print") as display:
            result = run_call(self.url, payload, "test-token")
        self.assertEqual("denied", result["status"])
        printed = "\n".join(str(call) for call in display.call_args_list)
        for text in ("send_email", "mentor@example.com", "题目", "完整正文"):
            self.assertIn(text, printed)
        self.assertEqual([], list(self.outbox.iterdir()))

    def test_audit_failure_on_transport_rejection_returns_500(self):
        with patch.object(self.gateway, "_audit", side_effect=OSError("disk full")):
            with self.assertRaises(HTTPError) as exc:
                self.raw_post("/tool-calls", "{invalid")
        self.assertEqual(500, exc.exception.code)
        self.assertEqual("unknown", json.load(exc.exception)["status"])


class UserConfigurationTests(unittest.TestCase):
    def test_invalid_user_configurations_are_rejected(self):
        for config in ({}, [], {"": "user:a"}, {"a b": "user:a"}, {"a": ""}, {"é": "user:a"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_users(config)

    def test_multiple_tokens_can_represent_different_users_or_same_user(self):
        validate_users({"a": "user:alice", "b": "user:bob", "a2": "user:alice"})


if __name__ == "__main__":
    unittest.main()
