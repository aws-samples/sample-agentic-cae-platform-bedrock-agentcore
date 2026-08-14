"""Unit tests for the Cognito-aligned password policy (Requirement 9.4).

These cover specific representative examples and boundary cases that complement
the property-based tests in ``tests/property/test_property_14_15_authentication``.
"""

from __future__ import annotations

import pytest

from shared.auth.password_policy import (
    MAX_LENGTH,
    MIN_LENGTH,
    check_password,
    is_valid_password,
)


@pytest.mark.parametrize(
    "password",
    [
        "Abcdef1!",  # exactly MIN_LENGTH, all classes
        "Str0ng#Password",
        "aB3$aB3$aB3$",
        "A" * 124 + "a1!x",  # long but within MAX_LENGTH
    ],
)
def test_valid_passwords_accepted(password: str) -> None:
    assert is_valid_password(password) is True
    assert check_password(password).valid is True
    assert check_password(password).violations == []


def test_boundary_min_length_valid() -> None:
    pw = "Abcd1!ef"
    assert len(pw) == MIN_LENGTH
    assert is_valid_password(pw) is True


def test_boundary_max_length_valid() -> None:
    body = ("Aa1!" * (MAX_LENGTH // 4))
    assert len(body) == MAX_LENGTH
    assert is_valid_password(body) is True


@pytest.mark.parametrize(
    ("password", "violation"),
    [
        ("Ab1!ef", "too_short"),  # 6 chars
        ("Aa1!" * 40, "too_long"),  # 160 chars
        ("abcdef1!", "missing_uppercase"),
        ("ABCDEF1!", "missing_lowercase"),
        ("Abcdefg!", "missing_digit"),
        ("Abcdefg1", "missing_special"),
    ],
)
def test_invalid_passwords_report_violation(password: str, violation: str) -> None:
    assert is_valid_password(password) is False
    result = check_password(password)
    assert result.valid is False
    assert violation in result.violations


def test_none_password_is_invalid() -> None:
    assert is_valid_password(None) is False
    result = check_password(None)
    assert result.valid is False
    assert "missing" in result.violations


def test_whitespace_does_not_count_as_special() -> None:
    # Space is not in the special-character set.
    pw = "Abcdef 1"
    assert is_valid_password(pw) is False
    assert "missing_special" in check_password(pw).violations
