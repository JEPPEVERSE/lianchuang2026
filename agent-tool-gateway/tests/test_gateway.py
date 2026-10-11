from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server.gateway.service import GatewayService
from server.__main__ import make_server
from client.__main__ import run_call


class GatewayFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 11, tzinfo=timezone.utc)
        self.gateway = GatewayService(self.root / "sandbox", self.root / "audit.jsonl",
                                      clock=lambda: self.now)
        self.notes = self.root / "sandbox/notes"
        (self.notes / "todo.md").write_text("buy milk", encoding="utf-8")
        (self.notes / "draft.txt").write_text("draft", encoding="utf-8")
        self.outbox = self.root / "sandbox/outbox"

    def call(self, name="read_note", arguments=None, call_id="call_1", user="user:demo"):
        return self.gateway.submit({"call_id": call_id, "tool_name": name,
                                    "arguments": arguments if arguments is not None else {"path": "todo.md"}}, user)

    def email(self, call_id="mail_call"):
        return self.call("send_email", {"to": "mentor@example.com", "subject": "作业链接",
                                        "body": "文档链接"}, call_id)

    def decide(self, response, action="approve", user="user:demo"):
        return self.gateway.decide(response["approval_id"], {"action": action}, user)

    def events(self):
        return [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]


class GatewayTests(GatewayFixture, unittest.TestCase):
    def test_read_allowed(self):
        result = self.call()
        self.assertEqual(("allow", "executed", "buy milk"),
                         (result["decision"], result["status"], result["result"]["content"]))

    def test_email_has_no_side_effect_before_approval(self):
        pending = self.email()
        self.assertEqual("pending", pending["status"])
        self.assertEqual([], list(self.outbox.iterdir()))
        result = self.decide(pending)
        self.assertEqual("executed", result["status"])
        files = list(self.outbox.iterdir())
        self.assertEqual(1, len(files))
        self.assertEqual("mentor@example.com", json.loads(files[0].read_text())["to"])

    def test_safe_delete_also_requires_approval(self):
        pending = self.call("delete_note", {"path": "draft.txt"})
        self.assertEqual("require_approval", pending["decision"])
        self.assertTrue((self.notes / "draft.txt").exists())
        self.assertEqual("executed", self.decide(pending)["status"])
        self.assertFalse((self.notes / "draft.txt").exists())

    def test_rejected_call_cannot_execute(self):
        pending = self.email()
        self.assertEqual("denied", self.decide(pending, "deny")["status"])
        self.assertEqual("denied", self.decide(pending)["status"])
        self.assertEqual([], list(self.outbox.iterdir()))

    def test_approval_only_consumed_once(self):
        pending = self.email()
        self.decide(pending)
        self.assertEqual("denied", self.decide(pending)["status"])
        self.assertEqual(1, len(list(self.outbox.iterdir())))

    def test_concurrent_approval_executes_once(self):
        pending = self.email()
        barrier = threading.Barrier(2)
        def approve():
            barrier.wait()
            return self.decide(pending)["status"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: approve(), range(2)))
        self.assertCountEqual(["executed", "denied"], results)
        self.assertEqual(1, len(list(self.outbox.iterdir())))

    def test_unknown_tool_and_invalid_parameters(self):
        for index, (name, args) in enumerate([
            ("shell", {}), ("read_note", {}), ("read_note", {"path": 12}),
            ("read_note", {"path": "todo.md", "requires_approval": False}),
            ("send_email", {"to": "invalid", "subject": "a", "body": "b"}),
        ]):
            with self.subTest(name=name, args=args):
                self.assertEqual("deny", self.call(name, args, str(index))["decision"])

    def test_agent_cannot_supply_policy_or_identity(self):
        for field in ("policy", "user_id", "decision"):
            payload = {"call_id": "x", "tool_name": "read_note", "arguments": {"path": "todo.md"}, field: "allow"}
            self.assertEqual("deny", self.gateway.submit(payload, "user:demo")["decision"])

    def test_traversal_and_absolute_paths_denied(self):
        outside = self.root / "config.json"
        outside.write_text("keep")
        for index, path in enumerate(["../../config.json", str(outside), "../notes/todo.md", "a/../todo.md"]):
            self.assertEqual("deny", self.call("delete_note", {"path": path}, str(index))["decision"])
        self.assertEqual("keep", outside.read_text())

    def test_file_and_directory_symlinks_denied(self):
        outside = self.root / "secret.txt"
        outside.write_text("secret")
        (self.notes / "link.txt").symlink_to(outside)
        (self.notes / "escape").symlink_to(self.root, target_is_directory=True)
        for index, path in enumerate(["link.txt", "escape/secret.txt"]):
            for name in ("read_note", "delete_note"):
                self.assertEqual("deny", self.call(name, {"path": path}, f"{name}{index}")["decision"])
        self.assertEqual("secret", outside.read_text())

    def test_file_replaced_by_symlink_before_confirmation(self):
        pending = self.call("delete_note", {"path": "draft.txt"})
        outside = self.root / "secret.txt"
        outside.write_text("keep")
        (self.notes / "draft.txt").unlink()
        (self.notes / "draft.txt").symlink_to(outside)
        self.assertEqual("deny", self.decide(pending)["decision"])
        self.assertEqual("keep", outside.read_text())

    def test_outbox_symlink_swap_cannot_write_outside(self):
        pending = self.email()
        self.outbox.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.outbox.symlink_to(outside, target_is_directory=True)
        self.assertEqual("failed", self.decide(pending)["status"])
        self.assertEqual([], list(outside.iterdir()))

    def test_expired_approval_denied(self):
        pending = self.email()
        self.now += timedelta(seconds=300)
        self.assertEqual("deny", self.decide(pending)["decision"])
        self.assertEqual([], list(self.outbox.iterdir()))

    def test_wrong_user_cannot_confirm(self):
        pending = self.email()
        self.assertEqual("deny", self.decide(pending, user="user:other")["decision"])
        self.assertEqual("executed", self.decide(pending)["status"])

    def test_confirmation_cannot_replace_arguments(self):
        pending = self.email()
        result = self.gateway.decide(pending["approval_id"],
                                     {"action": "approve", "arguments": {"to": "attacker@example.com"}}, "user:demo")
        self.assertEqual("deny", result["decision"])
        self.assertEqual([], list(self.outbox.iterdir()))

    def test_stored_arguments_and_tool_are_bound(self):
        for index, field in enumerate(("arguments", "tool_name")):
            pending = self.email(str(index))
            record = self.gateway._approvals[pending["approval_id"]]
            if field == "arguments":
                record.arguments["to"] = "attacker@example.com"
            else:
                record.tool_name = "read_note"
            self.assertEqual("deny", self.decide(pending)["decision"])
        self.assertEqual([], list(self.outbox.iterdir()))

    def test_duplicate_call_returns_cached_result(self):
        pending = self.email()
        self.decide(pending)
        replay = self.email()
        self.assertEqual("executed", replay["status"])
        self.assertEqual(1, len(list(self.outbox.iterdir())))
        self.assertEqual("deny", self.call("read_note", call_id="mail_call")["decision"])
        self.assertEqual("deny", self.call("send_email", pending["arguments"], "mail_call", "user:other")["decision"])

    def test_audit_has_decisions_confirmation_and_execution(self):
        self.call()
        pending = self.email()
        self.decide(pending)
        self.call("delete_note", {"path": "../../config.json"}, "escape")
        events = self.events()
        self.assertTrue({"allow", "require_approval", "deny"} <= {e["decision"] for e in events})
        self.assertTrue(any(e["user_action"] == "approve" for e in events))
        self.assertEqual(2, sum(e["event"] == "execution_finished" for e in events))
        for event in events:
            self.assertTrue({"timestamp", "user_id", "call_id", "tool_name", "decision",
                             "approval_id", "execution_result"} <= event.keys())

    def test_audit_failure_does_not_retry_side_effect(self):
        pending = self.email()
        original = self.gateway._audit
        def fail_after_execution(**kwargs):
            if kwargs.get("event") == "execution_finished":
                raise OSError("disk full")
            original(**kwargs)
        with patch.object(self.gateway, "_audit", side_effect=fail_after_execution):
            with self.assertRaises(OSError):
                self.decide(pending)
        self.assertEqual("unknown", self.email()["status"])
        self.assertEqual("denied", self.decide(pending)["status"])
        self.assertEqual(1, len(list(self.outbox.iterdir())))


class HttpTests(GatewayFixture, unittest.TestCase):
    # Only HTTP tests below: do not inherit/run the service suite twice.
    def setUp(self):
        super().setUp()
        self.server = make_server(self.gateway, {"test-token": "user:demo", "other-token": "user:other"}, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def raw_post(self, path, data, token="test-token"):
        request = Request(self.url + path, data=data.encode(),
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_http_requires_client_credential(self):
        with self.assertRaises(HTTPError) as exc:
            self.raw_post("/tool-calls", "{}", "bad-token")
        self.assertEqual(401, exc.exception.code)

    def test_http_rejects_duplicate_fields_and_nan(self):
        for body in ('{"action":"approve","action":"deny"}', '{"number":NaN}', '{invalid'):
            with self.assertRaises(HTTPError) as exc:
                self.raw_post("/tool-calls", body)
            self.assertEqual(400, exc.exception.code)

    def test_client_full_demo_and_real_confirmation(self):
        for call_id, name, arguments in [
            ("read", "read_note", {"path": "todo.md"}),
            ("mail", "send_email", {"to": "mentor@example.com", "subject": "a", "body": "b"}),
            ("escape", "delete_note", {"path": "../../config.json"}),
        ]:
            with patch("builtins.input", return_value="approve"), patch("builtins.print"):
                result = run_call(self.url, {"call_id": call_id, "tool_name": name,
                                            "arguments": arguments}, "test-token")
            self.assertEqual("denied" if call_id == "escape" else "executed", result["status"])
        self.assertEqual(1, len(list(self.outbox.iterdir())))

    def test_http_identity_comes_from_token(self):
        pending = self.email()
        result = self.raw_post(f"/approvals/{pending['approval_id']}/decision", '{"action":"approve"}', "other-token")
        self.assertEqual("deny", result["decision"])
        self.assertEqual([], list(self.outbox.iterdir()))


if __name__ == "__main__":
    unittest.main()
