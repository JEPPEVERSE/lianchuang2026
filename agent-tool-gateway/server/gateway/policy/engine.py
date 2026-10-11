from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase

from .errors import PolicyValidationError
from .models import AccessRequest, PolicyDecision, PolicyRule


def _matches_value(value: str, candidates: tuple[str, ...]) -> bool:
    return "*" in candidates or value in candidates


def _matches(rule: PolicyRule, request: AccessRequest, sensitivity: str) -> bool:
    return (
        _matches_value(request.principal_id, rule.principal_ids)
        and _matches_value(request.agent_id, rule.agent_ids)
        and _matches_value(request.purpose, rule.purposes)
        and _matches_value(request.action, rule.actions)
        and _matches_value(request.destination, rule.destinations)
        and _matches_value(sensitivity, rule.sensitivities)
        and any(
            fnmatchcase(request.resource_id, pattern)
            for pattern in rule.resource_patterns
        )
    )


def _allows_fields(rule: PolicyRule, fields: tuple[str, ...]) -> bool:
    return "*" in rule.allowed_fields or set(fields) <= set(rule.allowed_fields)


class PolicyEngine:
    """Evaluate immutable rules with deny precedence and default deny."""

    def __init__(self, rules: list[PolicyRule] | tuple[PolicyRule, ...]) -> None:
        self._rules = tuple(rules)

    def evaluate(
        self,
        request: AccessRequest,
        *,
        sensitivity: str,
        now: datetime | None = None,
    ) -> PolicyDecision:
        issued_at = now or datetime.now(timezone.utc)
        if issued_at.tzinfo is None:
            raise PolicyValidationError("policy evaluation time must include a timezone")
        matching = [
            rule for rule in self._rules if _matches(rule, request, sensitivity)
        ]
        denied = next((rule for rule in matching if rule.effect == "deny"), None)
        if denied is not None:
            return PolicyDecision(
                request=request,
                allowed=False,
                reason="explicit deny rule matched",
                rule_id=denied.rule_id,
                granted_fields=(),
                issued_at=issued_at,
                expires_at=None,
            )

        allowed = next(
            (
                rule
                for rule in matching
                if rule.effect == "allow" and _allows_fields(rule, request.fields)
            ),
            None,
        )
        if allowed is None:
            return PolicyDecision(
                request=request,
                allowed=False,
                reason="no allow rule covers this request",
                rule_id=None,
                granted_fields=(),
                issued_at=issued_at,
                expires_at=None,
            )

        lifetime = min(request.ttl_seconds, allowed.max_ttl_seconds)
        return PolicyDecision(
            request=request,
            allowed=True,
            reason="allow rule matched",
            rule_id=allowed.rule_id,
            granted_fields=request.fields,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=lifetime),
        )
