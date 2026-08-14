"""Cognito-aligned password policy validation (Requirement 9.4).

The Auth_Service accepts passwords of 8 to 128 characters that contain at
least one uppercase letter, one lowercase letter, one digit, and one special
(non-alphanumeric) character. This module provides a single source of truth
for that policy so the CDK Cognito User Pool configuration and any
server-side checks agree on what constitutes an acceptable password.

The policy is intentionally simple and dependency-free so it can be reused by
Lambda authorizers, tests, and validation utilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Length bounds (inclusive) mandated by Requirement 9.4.
MIN_LENGTH = 8
MAX_LENGTH = 128

# A "special character" is any character that is not a letter, digit, or
# whitespace. Whitespace is not counted as satisfying the special-character
# requirement.
_SPECIAL_CHARS = frozenset(
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
)


def _has_upper(password: str) -> bool:
    return any(c.isupper() for c in password)


def _has_lower(password: str) -> bool:
    return any(c.islower() for c in password)


def _has_digit(password: str) -> bool:
    return any(c.isdigit() for c in password)


def _has_special(password: str) -> bool:
    return any(c in _SPECIAL_CHARS for c in password)


def is_valid_password(password: str | None) -> bool:
    """Return ``True`` iff ``password`` satisfies the full policy.

    A valid password:

    - is between :data:`MIN_LENGTH` and :data:`MAX_LENGTH` characters (inclusive),
    - contains at least one uppercase letter,
    - contains at least one lowercase letter,
    - contains at least one digit,
    - contains at least one special (non-alphanumeric, non-whitespace) character.

    Args:
        password: The candidate password. ``None`` is treated as invalid.

    Returns:
        ``True`` when every rule is satisfied, otherwise ``False``.
    """
    if password is None:
        return False
    if not (MIN_LENGTH <= len(password) <= MAX_LENGTH):
        return False
    return (
        _has_upper(password)
        and _has_lower(password)
        and _has_digit(password)
        and _has_special(password)
    )


@dataclass(frozen=True)
class PasswordPolicyResult:
    """Detailed outcome of validating a password against the policy.

    Attributes:
        valid: ``True`` only when the password satisfies every rule.
        violations: Stable identifiers for each failed rule (empty when valid).
    """

    valid: bool
    violations: list[str] = field(default_factory=list)


def check_password(password: str | None) -> PasswordPolicyResult:
    """Validate a password and enumerate any policy violations.

    The returned :class:`PasswordPolicyResult` lists a stable identifier for
    every failed rule, which is useful for surfacing actionable feedback to a
    user during sign-up.

    Args:
        password: The candidate password. ``None`` is treated as invalid.

    Returns:
        A :class:`PasswordPolicyResult`. ``valid`` is ``True`` iff
        ``violations`` is empty, and both agree with :func:`is_valid_password`.
    """
    violations: list[str] = []

    if password is None:
        return PasswordPolicyResult(valid=False, violations=["missing"])

    length = len(password)
    if length < MIN_LENGTH:
        violations.append("too_short")
    if length > MAX_LENGTH:
        violations.append("too_long")
    if not _has_upper(password):
        violations.append("missing_uppercase")
    if not _has_lower(password):
        violations.append("missing_lowercase")
    if not _has_digit(password):
        violations.append("missing_digit")
    if not _has_special(password):
        violations.append("missing_special")

    return PasswordPolicyResult(valid=not violations, violations=violations)


__all__ = [
    "MAX_LENGTH",
    "MIN_LENGTH",
    "PasswordPolicyResult",
    "check_password",
    "is_valid_password",
]
