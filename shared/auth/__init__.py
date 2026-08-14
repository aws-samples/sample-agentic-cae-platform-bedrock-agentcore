"""Shared authentication utilities for Lambda and WebSocket authorization.

Exposes JWT token validation against a Cognito User Pool and a helper for the
Cognito refresh-token flow. See ``token_validator`` for details.
"""

from .password_policy import (
    MAX_LENGTH,
    MIN_LENGTH,
    PasswordPolicyResult,
    check_password,
    is_valid_password,
)
from .token_validator import (
    AuthError,
    AuthResult,
    RefreshResult,
    TokenValidator,
    TokenValidatorConfig,
    refresh_access_token,
    validate_token,
)

__all__ = [
    "MAX_LENGTH",
    "MIN_LENGTH",
    "AuthError",
    "AuthResult",
    "PasswordPolicyResult",
    "RefreshResult",
    "TokenValidator",
    "TokenValidatorConfig",
    "check_password",
    "is_valid_password",
    "refresh_access_token",
    "validate_token",
]
