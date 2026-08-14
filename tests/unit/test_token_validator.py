"""Unit tests for the shared JWT token validation middleware.

These tests mint real RS256-signed JWTs using an in-test RSA key pair and
inject a fake JWKS client so no network access to Cognito is required. The
Cognito refresh flow is exercised with a fake ``cognito-idp`` client.

Covers Requirements 9.2 (signature/expiration verification), 9.3 (401 for
missing/malformed/expired tokens), and 9.5 (refresh flow).
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.auth.token_validator import (
    AuthError,
    RefreshResult,
    TokenValidator,
    TokenValidatorConfig,
    refresh_access_token,
    validate_token,
)

REGION = "us-east-1"
USER_POOL_ID = "us-east-1_TestPool"
APP_CLIENT_ID = "test-app-client-id"
KID = "test-key-id"


# --------------------------------------------------------------------------- #
# Test fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    """A stable RSA key pair for signing/verifying test tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


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


@pytest.fixture
def config() -> TokenValidatorConfig:
    return TokenValidatorConfig(
        user_pool_id=USER_POOL_ID,
        region=REGION,
        app_client_id=APP_CLIENT_ID,
    )


@pytest.fixture
def validator(config: TokenValidatorConfig, rsa_key: rsa.RSAPrivateKey) -> TokenValidator:
    return TokenValidator(config, jwk_client=_FakeJWKClient(rsa_key.public_key()))


def _mint_token(
    rsa_key: rsa.RSAPrivateKey,
    *,
    issuer: str | None = None,
    token_use: str = "access",
    client_id: str | None = APP_CLIENT_ID,
    audience: str | None = APP_CLIENT_ID,
    exp_offset: int = 3600,
    include_exp: bool = True,
) -> str:
    """Mint a signed RS256 JWT for testing."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-123",
        "iss": issuer if issuer is not None else _issuer(),
        "token_use": token_use,
        "iat": now,
    }
    if include_exp:
        claims["exp"] = now + exp_offset
    if token_use == "access" and client_id is not None:  # nosec B105
        claims["client_id"] = client_id
    if token_use == "id" and audience is not None:  # nosec B105
        claims["aud"] = audience

    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": KID})


def _issuer() -> str:
    return f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"


# --------------------------------------------------------------------------- #
# Valid token
# --------------------------------------------------------------------------- #


def test_valid_access_token_is_accepted(validator: TokenValidator, rsa_key: rsa.RSAPrivateKey) -> None:
    token = _mint_token(rsa_key, token_use="access")  # nosec B106

    result = validator.validate(token)

    assert result.authorized is True
    assert result.status_code == 200
    assert result.error is None
    assert result.claims is not None
    assert result.claims["sub"] == "user-123"


def test_valid_id_token_is_accepted(validator: TokenValidator, rsa_key: rsa.RSAPrivateKey) -> None:
    token = _mint_token(rsa_key, token_use="id")  # nosec B106

    result = validator.validate(token)

    assert result.authorized is True
    assert result.status_code == 200


def test_bearer_prefix_is_stripped(validator: TokenValidator, rsa_key: rsa.RSAPrivateKey) -> None:
    token = _mint_token(rsa_key)

    result = validator.validate(f"Bearer {token}")

    assert result.authorized is True


# --------------------------------------------------------------------------- #
# Missing / malformed tokens -> 401
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", [None, "", "   ", "Bearer ", "Bearer    "])
def test_missing_token_rejected_with_401(validator: TokenValidator, raw: str | None) -> None:
    result = validator.validate(raw)

    assert result.authorized is False
    assert result.status_code == 401
    assert result.error is AuthError.MISSING_TOKEN
    assert result.claims is None


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-jwt",
        "abc.def",  # too few segments
        "aaa.bbb.ccc",  # not valid base64 JWT segments
        "Bearer garbage.token.value",
    ],
)
def test_malformed_token_rejected_with_401(validator: TokenValidator, raw: str) -> None:
    result = validator.validate(raw)

    assert result.authorized is False
    assert result.status_code == 401
    assert result.error in {AuthError.MALFORMED_TOKEN, AuthError.SIGNING_KEY_ERROR}


# --------------------------------------------------------------------------- #
# Expired token -> 401
# --------------------------------------------------------------------------- #


def test_expired_token_rejected_with_401(validator: TokenValidator, rsa_key: rsa.RSAPrivateKey) -> None:
    token = _mint_token(rsa_key, exp_offset=-3600)  # expired an hour ago

    result = validator.validate(token)

    assert result.authorized is False
    assert result.status_code == 401
    assert result.error is AuthError.EXPIRED_TOKEN


# --------------------------------------------------------------------------- #
# Wrong issuer -> 401
# --------------------------------------------------------------------------- #


def test_wrong_issuer_rejected_with_401(validator: TokenValidator, rsa_key: rsa.RSAPrivateKey) -> None:
    token = _mint_token(
        rsa_key,
        issuer="https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Other",
    )

    result = validator.validate(token)

    assert result.authorized is False
    assert result.status_code == 401
    assert result.error is AuthError.INVALID_ISSUER


# --------------------------------------------------------------------------- #
# Wrong signature -> 401
# --------------------------------------------------------------------------- #


def test_wrong_signature_rejected_with_401(config: TokenValidatorConfig) -> None:
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifying_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _mint_token(signing_key)

    # Validator trusts a *different* public key than the one that signed it.
    validator = TokenValidator(config, jwk_client=_FakeJWKClient(verifying_key.public_key()))
    result = validator.validate(token)

    assert result.authorized is False
    assert result.status_code == 401
    assert result.error is AuthError.INVALID_SIGNATURE


# --------------------------------------------------------------------------- #
# Wrong audience / client id -> 401
# --------------------------------------------------------------------------- #


def test_wrong_client_id_rejected_with_401(validator: TokenValidator, rsa_key: rsa.RSAPrivateKey) -> None:
    token = _mint_token(rsa_key, token_use="access", client_id="some-other-client")  # nosec B106

    result = validator.validate(token)

    assert result.authorized is False
    assert result.error is AuthError.INVALID_AUDIENCE


# --------------------------------------------------------------------------- #
# Config from environment
# --------------------------------------------------------------------------- #


def test_config_from_env_reads_variables() -> None:
    env = {
        "COGNITO_USER_POOL_ID": USER_POOL_ID,
        "AWS_REGION": REGION,
        "COGNITO_APP_CLIENT_ID": APP_CLIENT_ID,
    }
    cfg = TokenValidatorConfig.from_env(env)

    assert cfg.user_pool_id == USER_POOL_ID
    assert cfg.region == REGION
    assert cfg.app_client_id == APP_CLIENT_ID
    assert cfg.issuer == _issuer()
    assert cfg.jwks_uri == f"{_issuer()}/.well-known/jwks.json"


def test_config_from_env_derives_region_from_pool_id() -> None:
    env = {"COGNITO_USER_POOL_ID": "eu-west-1_abc"}
    cfg = TokenValidatorConfig.from_env(env)
    assert cfg.region == "eu-west-1"


def test_config_from_env_missing_pool_id_raises() -> None:
    with pytest.raises(ValueError):
        TokenValidatorConfig.from_env({"AWS_REGION": REGION})


def test_validate_token_missing_config_returns_401() -> None:
    # No config passed and no env -> configuration error surfaced as 401.
    result = validate_token("Bearer abc.def.ghi", config=None)
    # from_env may raise ValueError -> mapped to CONFIGURATION_ERROR, OR env
    # happens to be set; either way it must not raise and be a clean result.
    assert result.status_code == 401
    assert result.authorized is False


# --------------------------------------------------------------------------- #
# Refresh flow (Requirement 9.5)
# --------------------------------------------------------------------------- #


class _FakeCognitoClient:
    """Fake cognito-idp client for the refresh flow."""

    def __init__(self, result: dict[str, Any] | None = None, raise_exc: Exception | None = None) -> None:
        self._result = result or {}
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def initiate_auth(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._result


def test_refresh_with_valid_token_issues_new_access_token(config: TokenValidatorConfig) -> None:
    client = _FakeCognitoClient(
        result={
            "AuthenticationResult": {
                "AccessToken": "new-access-token",
                "IdToken": "new-id-token",
                "ExpiresIn": 3600,
                "TokenType": "Bearer",
            }
        }
    )

    result = refresh_access_token("valid-refresh-token", config=config, cognito_client=client)

    assert isinstance(result, RefreshResult)
    assert result.succeeded is True
    assert result.access_token == "new-access-token"  # nosec B105 - test assertion, not a real token
    assert result.expires_in == 3600
    # Verify the refresh flow parameters were sent correctly.
    assert client.calls[0]["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert client.calls[0]["ClientId"] == APP_CLIENT_ID
    assert client.calls[0]["AuthParameters"]["REFRESH_TOKEN"] == "valid-refresh-token"


def test_refresh_with_missing_token_fails_cleanly(config: TokenValidatorConfig) -> None:
    result = refresh_access_token(None, config=config, cognito_client=_FakeCognitoClient())

    assert result.succeeded is False
    assert result.error is AuthError.MISSING_TOKEN


def test_refresh_with_invalid_token_fails_cleanly(config: TokenValidatorConfig) -> None:
    client = _FakeCognitoClient(raise_exc=RuntimeError("NotAuthorizedException"))

    result = refresh_access_token("expired-refresh-token", config=config, cognito_client=client)

    assert result.succeeded is False
    assert result.error is AuthError.EXPIRED_TOKEN
    assert result.access_token is None


def test_refresh_without_app_client_id_fails_cleanly() -> None:
    cfg = TokenValidatorConfig(user_pool_id=USER_POOL_ID, region=REGION, app_client_id=None)

    result = refresh_access_token("token", config=cfg, cognito_client=_FakeCognitoClient())

    assert result.succeeded is False
    assert result.error is AuthError.CONFIGURATION_ERROR
