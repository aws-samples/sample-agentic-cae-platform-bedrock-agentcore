"""Property-based tests for the authentication layer.

Covers two correctness properties from the design document:

- **Property 14: Token Validation Rejects Invalid Credentials** — For any
  malformed token (garbage string / wrong segment count) or any expired JWT,
  ``TokenValidator.validate`` SHALL return ``authorized=False`` with
  ``status_code == 401``. Malformed tokens map to ``MALFORMED_TOKEN`` or
  ``SIGNING_KEY_ERROR``; expired-but-otherwise-valid tokens map to
  ``EXPIRED_TOKEN``.
  **Validates: Requirements 9.3**

- **Property 15: Password Policy Enforcement** — For any password string, the
  policy validator SHALL accept it iff it is 8–128 characters and contains at
  least one uppercase letter, one lowercase letter, one digit, and one special
  character. The test recomputes the policy independently and asserts the two
  agree on every generated input.
  **Validates: Requirements 9.4**

Property 14 mints real RS256-signed JWTs with an in-test RSA key pair and
injects a fake JWKS client, reusing the pattern from the token_validator unit
tests, so no network access to Cognito is required.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from shared.auth.password_policy import (
    MAX_LENGTH,
    MIN_LENGTH,
    check_password,
    is_valid_password,
)
from shared.auth.token_validator import (
    AuthError,
    TokenValidator,
    TokenValidatorConfig,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers for Property 14 (reused RSA + fake-JWKS pattern).
# ---------------------------------------------------------------------------

REGION = "us-east-1"
USER_POOL_ID = "us-east-1_TestPool"
APP_CLIENT_ID = "test-app-client-id"
KID = "test-key-id"

_UNAUTHORIZED = 401

# One stable RSA key pair for the whole module. Generating RSA keys is slow, so
# reuse it across all examples rather than minting a new key per case.
_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _issuer() -> str:
    return f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"


class _FakeSigningKey:
    """Mimics PyJWKClient's returned signing key (exposes ``.key``)."""

    def __init__(self, key: Any) -> None:
        self.key = key


class _FakeJWKClient:
    """Fake JWKS client that always returns the same public key."""

    def __init__(self, public_key: Any) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:  # noqa: ARG002
        return _FakeSigningKey(self._public_key)


def _make_validator() -> TokenValidator:
    config = TokenValidatorConfig(
        user_pool_id=USER_POOL_ID,
        region=REGION,
        app_client_id=APP_CLIENT_ID,
    )
    return TokenValidator(config, jwk_client=_FakeJWKClient(_RSA_KEY.public_key()))


def _mint_expired_token(exp_offset: int) -> str:
    """Mint a well-formed RS256 JWT that expired ``-exp_offset`` seconds ago."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-123",
        "iss": _issuer(),
        "token_use": "access",
        "client_id": APP_CLIENT_ID,
        "iat": now + exp_offset - 1,
        "exp": now + exp_offset,  # exp_offset < 0 => already expired
    }
    return jwt.encode(claims, _RSA_KEY, algorithm="RS256", headers={"kid": KID})


# ---------------------------------------------------------------------------
# Property 14: Token Validation Rejects Invalid Credentials
# ---------------------------------------------------------------------------

# Strategy: arbitrary "garbage" strings, biased toward JWT-looking shapes with
# the wrong number of dot-separated segments.
_garbage_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40
)


@st.composite
def _malformed_token(draw) -> str:
    """Generate a non-empty token that cannot be a valid three-segment JWT.

    Produces either free-form garbage or a dot-joined sequence of segments
    whose count is not exactly 3 (the valid JWT segment count).
    """
    style = draw(st.integers(min_value=0, max_value=1))
    if style == 0:
        # Free-form garbage with no structural guarantees.
        token = draw(_garbage_text)
    else:
        # Wrong number of segments (0-2 or 4-6 dot-separated parts).
        n_segments = draw(st.sampled_from([1, 2, 4, 5, 6]))
        segments = draw(
            st.lists(
                st.text(
                    alphabet=st.characters(
                        whitelist_categories=("Lu", "Ll", "Nd"),
                    ),
                    min_size=1,
                    max_size=8,
                ),
                min_size=n_segments,
                max_size=n_segments,
            )
        )
        token = ".".join(segments)

    # Guard: never emit blank (that is a *missing* token, a different case),
    # and never accidentally emit an exactly-3-segment structure.
    assume(token.strip() != "")
    assume(token.count(".") != 2)
    return token


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(raw_token=_malformed_token())
def test_property_14_malformed_tokens_rejected_with_401(raw_token: str) -> None:
    """Any malformed token is rejected with a 401 and a malformed/key error.

    **Validates: Requirements 9.3**
    """
    validator = _make_validator()

    result = validator.validate(raw_token)

    assert result.authorized is False
    assert result.status_code == _UNAUTHORIZED
    assert result.claims is None
    assert result.error in {
        AuthError.MALFORMED_TOKEN,
        AuthError.SIGNING_KEY_ERROR,
    }, f"unexpected error {result.error} for token {raw_token!r}"


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(seconds_expired=st.integers(min_value=1, max_value=60 * 60 * 24 * 365))
def test_property_14_expired_tokens_rejected_with_401(seconds_expired: int) -> None:
    """Any otherwise-valid JWT whose exp is in the past is rejected with 401.

    **Validates: Requirements 9.3**
    """
    validator = _make_validator()
    token = _mint_expired_token(exp_offset=-seconds_expired)

    result = validator.validate(token)

    assert result.authorized is False
    assert result.status_code == _UNAUTHORIZED
    assert result.error is AuthError.EXPIRED_TOKEN
    assert result.claims is None


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(raw_token=st.sampled_from([None, "", "   ", "\t", "Bearer ", "Bearer    "]))
def test_property_14_missing_tokens_rejected_with_401(raw_token: str | None) -> None:
    """Missing/blank tokens are rejected with a 401 (MISSING_TOKEN).

    **Validates: Requirements 9.3**
    """
    validator = _make_validator()

    result = validator.validate(raw_token)

    assert result.authorized is False
    assert result.status_code == _UNAUTHORIZED
    assert result.error is AuthError.MISSING_TOKEN


# ---------------------------------------------------------------------------
# Property 15: Password Policy Enforcement
# ---------------------------------------------------------------------------

# Character classes used to reason about (and construct) passwords.
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"
_SPECIAL = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"


def _independent_is_valid(password: str) -> bool:
    """Re-derive the policy verdict independently of the implementation.

    A valid password is 8-128 chars and includes at least one uppercase, one
    lowercase, one digit, and one special (non-alphanumeric, non-whitespace)
    character.
    """
    if not (MIN_LENGTH <= len(password) <= MAX_LENGTH):
        return False
    has_upper = any(c.isupper() for c in password)
    has_lower = any(c.islower() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_special = any(c in _SPECIAL for c in password)
    return has_upper and has_lower and has_digit and has_special


# Broad strategy: fully arbitrary strings (mostly invalid) so rejection paths
# are heavily exercised.
_arbitrary_password = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=140,
)


@st.composite
def _valid_password(draw) -> str:
    """Generate a password that satisfies every policy rule.

    Guarantees exactly one character from each required class plus filler sized
    so the total length always lands within [MIN_LENGTH, MAX_LENGTH]. No
    truncation is applied, so required-class characters are never dropped.
    """
    required = [
        draw(st.sampled_from(_UPPER)),
        draw(st.sampled_from(_LOWER)),
        draw(st.sampled_from(_DIGITS)),
        draw(st.sampled_from(_SPECIAL)),
    ]
    # Four required chars are already present; size the filler so the total is
    # in [MIN_LENGTH, MAX_LENGTH] without any post-hoc truncation.
    filler = draw(
        st.text(
            alphabet=_UPPER + _LOWER + _DIGITS + _SPECIAL,
            min_size=MIN_LENGTH - len(required),
            max_size=MAX_LENGTH - len(required),
        )
    )
    raw = required + list(filler)
    # Shuffle so required classes are not always in a fixed prefix position.
    draw(st.randoms()).shuffle(raw)
    return "".join(raw)


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(password=_arbitrary_password)
def test_property_15_acceptance_matches_independent_policy(password: str) -> None:
    """The validator accepts a password iff it independently satisfies the policy.

    **Validates: Requirements 9.4**
    """
    expected = _independent_is_valid(password)
    actual = is_valid_password(password)
    assert actual == expected, (
        f"policy disagreement for {password!r}: "
        f"is_valid_password={actual}, expected={expected}"
    )
    # The detailed checker must agree with the boolean fast-path.
    assert check_password(password).valid == actual


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(password=_valid_password())
def test_property_15_compliant_passwords_are_accepted(password: str) -> None:
    """Any password satisfying all rules is accepted with no violations.

    **Validates: Requirements 9.4**
    """
    # Sanity: the generator really did satisfy the independent policy.
    assert _independent_is_valid(password)

    assert is_valid_password(password) is True
    result = check_password(password)
    assert result.valid is True
    assert result.violations == []


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    password=_valid_password(),
    missing=st.sampled_from(["upper", "lower", "digit", "special"]),
)
def test_property_15_removing_a_required_class_is_rejected(
    password: str, missing: str
) -> None:
    """Stripping any one required character class causes rejection.

    **Validates: Requirements 9.4**
    """
    class_chars = {
        "upper": _UPPER,
        "lower": _LOWER,
        "digit": _DIGITS,
        "special": _SPECIAL,
    }[missing]
    # The violation identifier reported by the policy for each stripped class.
    violation_id = {
        "upper": "missing_uppercase",
        "lower": "missing_lowercase",
        "digit": "missing_digit",
        "special": "missing_special",
    }[missing]
    stripped = "".join(c for c in password if c not in class_chars)
    # If stripping dropped us below the minimum length, the point still holds
    # (it becomes invalid), but keep the focus on the missing-class violation.
    assume(len(stripped) >= MIN_LENGTH)

    assert is_valid_password(stripped) is False
    result = check_password(stripped)
    assert result.valid is False
    assert violation_id in result.violations


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    base=_valid_password(),
    short_len=st.integers(min_value=0, max_value=MIN_LENGTH - 1),
)
def test_property_15_too_short_is_rejected(base: str, short_len: int) -> None:
    """Passwords shorter than the minimum length are rejected.

    **Validates: Requirements 9.4**
    """
    truncated = base[:short_len]
    assert is_valid_password(truncated) is False
    assert "too_short" in check_password(truncated).violations


@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@given(
    over_by=st.integers(min_value=1, max_value=40),
)
def test_property_15_too_long_is_rejected(over_by: int) -> None:
    """A password exceeding the maximum length is rejected even if well-formed.

    **Validates: Requirements 9.4**
    """
    # Build an over-length password that satisfies every character class so the
    # only violated rule is the maximum-length bound.
    length = MAX_LENGTH + over_by
    body = ("Aa1!" * ((length // 4) + 1))[:length]
    assert len(body) == length
    assert is_valid_password(body) is False
    assert "too_long" in check_password(body).violations
