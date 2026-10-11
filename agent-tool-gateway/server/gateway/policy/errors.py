class PolicyValidationError(ValueError):
    """Raised when a policy or request is malformed."""


class AccessDenied(PermissionError):
    """Raised when no policy grants the requested access."""
