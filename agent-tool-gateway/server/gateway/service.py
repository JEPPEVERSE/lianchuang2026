from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .policy.engine import PolicyEngine
from .policy.models import AccessRequest, PolicyRule
from ..tools.registry import ToolDefinition, ToolError, ToolRegistry


def canonical_hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class Approval:
    approval_id: str
    call_id: str
    user_id: str
    tool_name: str
    arguments: dict
    argument_hash: str
    expires_at: datetime
    used: bool = False
    rejected: bool = False


class GatewayService:
    """All decisions and tool execution pass through this trusted boundary.

    The lock serializes approval consumption and execution inside one process.
    Calls/approvals are in memory; the audit log is persistent.
    """

    def __init__(self, sandbox: Path, audit_path: Path, *, approval_ttl: int = 300,
                 clock: Callable[[], datetime] | None = None):
        self.registry = ToolRegistry(sandbox)
        self.audit_path = audit_path
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.approval_ttl = approval_ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._calls: dict[str, tuple[str, str, dict]] = {}
        self._approvals: dict[str, Approval] = {}
        # Adapted from Personal_AI: server-owned immutable policy rules.
        self.policy = PolicyEngine([
            PolicyRule(rule_id=f"sandbox-{name}", effect="allow",
                       agent_ids=("agent:demo",), purposes=("tool_execution",),
                       resource_patterns=(f"tool:{name}",), destinations=("local",),
                       sensitivities=("private",), allowed_fields=("arguments",),
                       actions=(action,))
            for name, action in (("read_note", "read"), ("send_email", "send"),
                                 ("delete_note", "delete"))
        ])

    def _audit(self, *, user_id: str, call_id: str | None, tool_name: str | None,
               decision: str, approval_id: str | None = None,
               user_action: str | None = None, execution_result: dict | None = None,
               reason: str | None = None, event: str = "decision") -> None:
        record = {"timestamp": self.clock().isoformat(), "user_id": user_id,
                  "call_id": call_id, "tool_name": tool_name, "decision": decision,
                  "approval_id": approval_id, "user_action": user_action,
                  "execution_result": execution_result, "reason": reason, "event": event}
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _deny(self, user_id: str, call_id: str | None, tool_name: str | None,
              reason: str, approval_id: str | None = None,
              user_action: str | None = None) -> dict:
        self._audit(user_id=user_id, call_id=call_id, tool_name=tool_name,
                    decision="deny", reason=reason, approval_id=approval_id,
                    user_action=user_action)
        return {"call_id": call_id, "decision": "deny", "status": "denied",
                "reason": reason, **({"approval_id": approval_id} if approval_id else {})}

    def _execute(self, tool: ToolDefinition, arguments: dict, *, user_id: str,
                 call_id: str, decision: str, approval_id: str | None = None) -> dict:
        # Record intent before side effects; failure to audit prevents execution.
        self._audit(user_id=user_id, call_id=call_id, tool_name=tool.name,
                    decision=decision, approval_id=approval_id, event="execution_started")
        try:
            result = tool.execute(arguments)
            response = {"call_id": call_id, "decision": decision,
                        "status": "executed", "result": result}
        except (OSError, UnicodeError, ToolError) as exc:
            response = {"call_id": call_id, "decision": decision, "status": "failed",
                        "reason": f"工具执行失败：{exc}"}
        if approval_id:
            response["approval_id"] = approval_id
        self._audit(user_id=user_id, call_id=call_id, tool_name=tool.name,
                    decision=decision, approval_id=approval_id,
                    execution_result=response, event="execution_finished")
        return response

    def submit(self, payload: dict, user_id: str) -> dict:
        with self._lock:
            return self._submit(payload, user_id)

    def _submit(self, payload: dict, user_id: str) -> dict:
        call_id = payload.get("call_id") if isinstance(payload, dict) else None
        name = payload.get("tool_name") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict)
            or set(payload) != {"call_id", "tool_name", "arguments"}
            or not isinstance(call_id, str) or not call_id.strip()
            or not isinstance(name, str) or not name.strip()
            or not isinstance(payload["arguments"], dict)):
            return self._deny(user_id, call_id if isinstance(call_id, str) else None,
                              name if isinstance(name, str) else None, "ToolCall 格式非法")
        fingerprint = canonical_hash(payload)
        previous = self._calls.get(call_id)
        if previous:
            owner, original_hash, response = previous
            if owner != user_id or original_hash != fingerprint:
                return self._deny(user_id, call_id, name, "call_id 已绑定另一用户或不同参数")
            self._audit(user_id=user_id, call_id=call_id, tool_name=name,
                        decision=response["decision"], approval_id=response.get("approval_id"),
                        execution_result=response if response["status"] != "pending" else None,
                        event="replayed")
            return deepcopy(response)
        tool = self.registry.get(name)
        arguments = deepcopy(payload["arguments"])
        try:
            if tool is None:
                raise ToolError("未知工具")
            self.registry.validate(tool, arguments)
            access = AccessRequest(principal_id=user_id, agent_id="agent:demo",
                                   action=tool.action, purpose="tool_execution",
                                   resource_id=f"tool:{name}", destination="local",
                                   fields=("arguments",))
            policy_decision = self.policy.evaluate(access, sensitivity="private", now=self.clock())
            if not policy_decision.allowed:
                raise ToolError(policy_decision.reason)
        except ToolError as exc:
            response = self._deny(user_id, call_id, name, str(exc))
        else:
            if tool.requires_approval:
                approval_id = f"approval_{uuid4().hex}"
                approval = Approval(approval_id, call_id, user_id, name, arguments,
                                    canonical_hash(arguments), self.clock() + timedelta(seconds=self.approval_ttl))
                response = {"call_id": call_id, "decision": "require_approval", "status": "pending",
                            "reason": tool.reason, "approval_id": approval_id, "tool_name": name,
                            "arguments": deepcopy(arguments), "expires_at": approval.expires_at.isoformat()}
                self._audit(user_id=user_id, call_id=call_id, tool_name=name,
                            decision="require_approval", approval_id=approval_id, reason=tool.reason)
                self._approvals[approval_id] = approval
            else:
                self._calls[call_id] = (user_id, fingerprint, {
                    "call_id": call_id, "decision": "allow", "status": "unknown",
                    "reason": "执行结果未确认，请检查审计记录",
                })
                response = self._execute(tool, arguments, user_id=user_id,
                                         call_id=call_id, decision="allow")
        self._calls[call_id] = (user_id, fingerprint, deepcopy(response))
        return response

    def decide(self, approval_id: str, payload: dict, user_id: str) -> dict:
        with self._lock:
            approval = self._approvals.get(approval_id)
            call_id = approval.call_id if approval and approval.user_id == user_id else None
            name = approval.tool_name if call_id else None
            if (not isinstance(payload, dict) or set(payload) != {"action"}
                    or payload["action"] not in ("approve", "deny")):
                return self._deny(user_id, call_id, name, "只允许提交 action: approve 或 deny", approval_id)
            action = payload["action"]
            if approval is None or approval.user_id != user_id:
                return self._deny(user_id, None, None, "确认不存在或不属于当前用户", approval_id, action)
            if approval.used or approval.rejected:
                return self._deny(user_id, call_id, name, "确认已使用或已拒绝", approval_id, action)
            if self.clock() >= approval.expires_at:
                return self._deny(user_id, call_id, name, "确认已过期", approval_id, action)
            if canonical_hash(approval.arguments) != approval.argument_hash:
                return self._deny(user_id, call_id, name, "确认参数发生变化", approval_id, action)
            tool = self.registry.get(approval.tool_name)
            if tool is None:
                return self._deny(user_id, call_id, name, "工具已不可用", approval_id, action)
            # Binding covers the complete tool call, not just the arguments.
            owner, fingerprint, _ = self._calls[approval.call_id]
            expected = canonical_hash({"call_id": approval.call_id, "tool_name": approval.tool_name,
                                       "arguments": approval.arguments})
            if owner != user_id or fingerprint != expected:
                return self._deny(user_id, call_id, name, "确认工具或参数发生变化", approval_id, action)
            if action == "deny":
                response = self._deny(user_id, call_id, name, "用户拒绝执行", approval_id, action)
                approval.rejected = True
            else:
                try:
                    self.registry.validate(tool, approval.arguments)
                except ToolError as exc:
                    return self._deny(user_id, call_id, name, str(exc), approval_id, action)
                self._audit(user_id=user_id, call_id=call_id, tool_name=name,
                            decision="require_approval", approval_id=approval_id,
                            user_action=action, event="user_decision")
                approval.used = True
                self._calls[call_id] = (owner, fingerprint, {
                    "call_id": call_id, "decision": "require_approval", "status": "unknown",
                    "approval_id": approval_id, "reason": "执行结果未确认，请检查审计记录",
                })
                response = self._execute(tool, approval.arguments, user_id=user_id,
                                         call_id=call_id, decision="require_approval", approval_id=approval_id)
            owner, fingerprint, _ = self._calls[call_id]
            self._calls[call_id] = (owner, fingerprint, deepcopy(response))
            return response
