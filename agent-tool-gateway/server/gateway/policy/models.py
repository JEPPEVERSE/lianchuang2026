from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .errors import PolicyValidationError

VALID_ACTIONS = {"read", "send", "delete"}
VALID_DESTINATIONS = {"local", "cloud", "tee"}
VALID_EFFECTS = {"allow", "deny"}
VALID_SENSITIVITIES = {"public", "private", "restricted"}


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PolicyValidationError(f"{name} must be a non-empty string")


def _string_tuple(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise PolicyValidationError(f"{name} must be a non-empty tuple")
    for value in values:
        _required(value, f"{name}[]")


@dataclass(frozen=True, slots=True)
class AccessRequest:
    principal_id: str
    agent_id: str
    action: str
    purpose: str
    resource_id: str
    destination: str
    fields: tuple[str, ...]
    ttl_seconds: int = 60

    def __post_init__(self) -> None:
        for name in ("principal_id", "agent_id", "purpose", "resource_id"):
            _required(getattr(self, name), name)
        if self.action not in VALID_ACTIONS:
            raise PolicyValidationError(f"unsupported action: {self.action}")
        if self.destination not in VALID_DESTINATIONS:
            raise PolicyValidationError(
                f"unsupported destination: {self.destination}"
            )
        _string_tuple(self.fields, "fields")
        if (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, int)
            or self.ttl_seconds <= 0
        ):
            raise PolicyValidationError("ttl_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class PolicyRule:
    rule_id: str
    effect: str
    agent_ids: tuple[str, ...]
    purposes: tuple[str, ...]
    resource_patterns: tuple[str, ...]
    destinations: tuple[str, ...]
    sensitivities: tuple[str, ...]
    allowed_fields: tuple[str, ...] = ("*",)
    actions: tuple[str, ...] = ("read",)
    principal_ids: tuple[str, ...] = ("*",)
    max_ttl_seconds: int = 60

    def __post_init__(self) -> None:
        _required(self.rule_id, "rule_id")
        if self.effect not in VALID_EFFECTS:
            raise PolicyValidationError(f"unsupported effect: {self.effect}")
        for name in (
            "agent_ids",
            "purposes",
            "resource_patterns",
            "destinations",
            "sensitivities",
            "allowed_fields",
            "actions",
            "principal_ids",
        ):
            _string_tuple(getattr(self, name), name)
        if not set(self.actions) <= VALID_ACTIONS:
            raise PolicyValidationError("rule contains an unsupported action")
        if not set(self.destinations) <= VALID_DESTINATIONS:
            raise PolicyValidationError("rule contains an unsupported destination")
        if not set(self.sensitivities) <= VALID_SENSITIVITIES:
            raise PolicyValidationError("rule contains an unsupported sensitivity")
        if (
            isinstance(self.max_ttl_seconds, bool)
            or not isinstance(self.max_ttl_seconds, int)
            or self.max_ttl_seconds <= 0
        ):
            raise PolicyValidationError("max_ttl_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    request: AccessRequest
    allowed: bool
    reason: str
    rule_id: str | None
    granted_fields: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime | None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: datetime
    request: AccessRequest
    decision: PolicyDecision
