"""JWT token validation middleware for Lambda and WebSocket authorization.

Validates Amazon Cognito-issued JWTs (access or id tokens) against a User Pool
by verifying the RS256 signature using the pool's JWKS, and checking the token
expiration, issuer, token use, and audience/client id. All validation entry
points return a structured :class:`AuthResult` rather than raising, so callers
can uniformly translate failures into a ``401`` response (Requirements 9.2,
9.3).

A helper, :func:`refresh_access_token`, implements the Cognito refresh-token
flow: given a valid refresh token it obtains a fresh access token without
requiring the user to re-authenticate (Requirement 9.5).

Configuration (User Pool id, region, app client id) is read from environment
variables and never hardcoded:

- ``COGNITO_USER_POOL_ID`` — the Cognito User Pool id (e.g. ``us-east-1_abc123``)
- ``AWS_REGION`` / ``COGNITO_REGION`` — the AWS region of the pool
- ``COGNITO_APP_CLIENT_ID`` — the app client id (audience / ``client_id``)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import jwt
from jwt import PyJWKClient

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

# HTTP status returned for every authentication failure.
_UNAUTHORIZED = 401
_OK = 200

# Cognito signs its tokens with RS256.
_ALGORITHMS = ("RS256",)


class AuthError(str, Enum):
    """Categories of authentication failure.

    Values are stable strings so they can be surfaced in structured error
    payloads and asserted against in tests.
    """

    MISSING_TOKEN = "missing_token"  # nosec B105 - error code constant, not a password
    MALFORMED_TOKEN = "malformed_token"  # nosec B105 - error code constant, not a password
    EXPIRED_TOKEN = "expired_token"  # nosec B105 - error code constant, not a password
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_ISSUER = "invalid_issuer"
    INVALID_AUDIENCE = "invalid_audience"
    INVALID_TOKEN_USE = "invalid_token_use"  # nosec B105 - error code constant, not a password
    CONFIGURATION_ERROR = "configuration_error"
    SIGNING_KEY_ERROR = "signing_key_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthResult:
    """Outcome of validating a JWT.

    Attributes:
        authorized: ``True`` only when the token passed every check.
        status_code: ``200`` when authorized, otherwise ``401``.
        claims: Decoded token claims when authorized, else ``None``.
        error: The :class:`AuthError` category on failure, else ``None``.
        message: Human-readable description of the outcome.
    """

    authorized: bool
    status_code: int
    claims: dict[str, Any] | None = None
    error: AuthError | None = None
    message: str = ""

    @classmethod
    def success(cls, claims: dict[str, Any]) -> AuthResult:
        return cls(
            authorized=True,
            status_code=_OK,
            claims=claims,
            error=None,
            message="Token is valid.",
        )

    @classmethod
    def failure(cls, error: AuthError, message: str) -> AuthResult:
        return cls(
            authorized=False,
            status_code=_UNAUTHORIZED,
            claims=None,
            error=error,
            message=message,
        )


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of a Cognito refresh-token exchange.

    Attributes:
        succeeded: ``True`` when a new access token was issued.
        access_token: The newly issued access token, if any.
        id_token: The newly issued id token, if any.
        expires_in: Access token lifetime in seconds, if provided.
        token_type: Token type returned by Cognito (typically ``Bearer``).
        error: An :class:`AuthError` category on failure, else ``None``.
        message: Human-readable description of the outcome.
    """

    succeeded: bool
    access_token: str | None = None
    id_token: str | None = None
    expires_in: int | None = None
    token_type: str | None = None
    error: AuthError | None = None
    message: str = ""


@dataclass(frozen=True)
class TokenValidatorConfig:
    """Configuration for validating Cognito tokens.

    Prefer :meth:`from_env` so the User Pool id, region, and app client id are
    sourced from environment variables rather than hardcoded.
    """

    user_pool_id: str
    region: str
    app_client_id: str | None = None
    # Optional list of accepted ``token_use`` values. Defaults to accepting
    # both access and id tokens.
    accepted_token_use: tuple[str, ...] = ("access", "id")
    # Clock-skew leeway (seconds) applied to expiration checks.
    leeway_seconds: int = 0

    @property
    def issuer(self) -> str:
        """The expected ``iss`` claim for tokens from this User Pool."""
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_uri(self) -> str:
        """The JWKS endpoint for this User Pool."""
        return f"{self.issuer}/.well-known/jwks.json"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TokenValidatorConfig:
        """Build a config from environment variables.

        Args:
            env: Optional mapping to read from (defaults to ``os.environ``).

        Returns:
            A populated :class:`TokenValidatorConfig`.

        Raises:
            ValueError: If a required variable is missing.
        """
        source = env if env is not None else dict(os.environ)

        user_pool_id = (source.get("COGNITO_USER_POOL_ID") or "").strip()
        region = (
            source.get("COGNITO_REGION")
            or source.get("AWS_REGION")
            or source.get("AWS_DEFAULT_REGION")
            or ""
        ).strip()
        app_client_id = (source.get("COGNITO_APP_CLIENT_ID") or "").strip() or None

        if not user_pool_id:
            raise ValueError("COGNITO_USER_POOL_ID environment variable is required")
        # Region can often be derived from the pool id prefix (e.g. us-east-1_x).
        if not region and "_" in user_pool_id:
            region = user_pool_id.split("_", 1)[0]
        if not region:
            raise ValueError(
                "AWS region is required (set AWS_REGION or COGNITO_REGION)"
            )

        return cls(
            user_pool_id=user_pool_id,
            region=region,
            app_client_id=app_client_id,
        )


def _strip_bearer(raw_token: str) -> str:
    """Remove an optional ``Bearer`` scheme prefix from an Authorization value.

    Only strips when ``Bearer`` is followed by whitespace (or is the entire
    value), so JWTs that merely begin with the letters ``bearer`` are left
    intact. A value of just ``"Bearer "`` collapses to an empty string.
    """
    token = raw_token.strip()
    if token.lower().startswith("bearer"):
        remainder = token[len("bearer") :]
        if remainder == "" or remainder[0].isspace():
            return remainder.strip()
    return token


class TokenValidator:
    """Validates Cognito JWTs against a User Pool's JWKS.

    The JWKS client is injectable to keep the class testable without network
    access: pass any object exposing ``get_signing_key_from_jwt(token)`` that
    returns an object with a ``.key`` attribute.
    """

    def __init__(
        self,
        config: TokenValidatorConfig,
        jwk_client: Any | None = None,
    ) -> None:
        self._config = config
        self._jwk_client = jwk_client

    @property
    def config(self) -> TokenValidatorConfig:
        return self._config

    def _get_jwk_client(self) -> Any:
        """Lazily construct a :class:`PyJWKClient` for the pool's JWKS URI."""
        if self._jwk_client is None:
            # PyJWKClient caches keys in-process, avoiding a fetch per call.
            self._jwk_client = PyJWKClient(self._config.jwks_uri, cache_keys=True)
        return self._jwk_client

    def validate(self, raw_token: str | None) -> AuthResult:
        """Validate a JWT and return a structured result (never raises).

        Args:
            raw_token: The token string, optionally prefixed with ``Bearer``.
                ``None`` or blank is treated as a missing token.

        Returns:
            An :class:`AuthResult`. ``authorized`` is ``True`` only when the
            signature, expiration, issuer, token use, and audience all pass.
        """
        if raw_token is None or not raw_token.strip():
            return AuthResult.failure(AuthError.MISSING_TOKEN, "No authorization token provided.")

        token = _strip_bearer(raw_token)
        if not token:
            return AuthResult.failure(AuthError.MISSING_TOKEN, "No authorization token provided.")

        # Resolve the signing key from the JWKS by matching the token's `kid`.
        try:
            signing_key = self._get_jwk_client().get_signing_key_from_jwt(token)
            key = getattr(signing_key, "key", signing_key)
        except jwt.exceptions.PyJWKClientError as exc:
            return AuthResult.failure(
                AuthError.SIGNING_KEY_ERROR,
                f"Unable to resolve signing key: {exc}",
            )
        except jwt.exceptions.DecodeError as exc:
            return AuthResult.failure(
                AuthError.MALFORMED_TOKEN,
                f"Malformed token: {exc}",
            )
        except Exception as exc:  # pragma: no cover - defensive catch-all
            return AuthResult.failure(
                AuthError.MALFORMED_TOKEN,
                f"Unable to parse token header: {exc}",
            )

        # Verify signature, expiration, and issuer. Audience is checked
        # manually below because Cognito access tokens carry `client_id`
        # rather than `aud`.
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(_ALGORITHMS),
                issuer=self._config.issuer,
                leeway=self._config.leeway_seconds,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": False,
                    "require": ["exp", "iss"],
                },
            )
        except jwt.ExpiredSignatureError:
            return AuthResult.failure(AuthError.EXPIRED_TOKEN, "Token has expired.")
        except jwt.InvalidIssuerError:
            return AuthResult.failure(
                AuthError.INVALID_ISSUER,
                f"Token issuer does not match expected pool issuer {self._config.issuer}.",
            )
        except jwt.InvalidSignatureError:
            return AuthResult.failure(
                AuthError.INVALID_SIGNATURE, "Token signature verification failed."
            )
        except jwt.exceptions.DecodeError as exc:
            return AuthResult.failure(AuthError.MALFORMED_TOKEN, f"Malformed token: {exc}")
        except jwt.InvalidTokenError as exc:
            return AuthResult.failure(
                AuthError.MALFORMED_TOKEN, f"Invalid token: {exc}"
            )

        token_use_result = self._check_token_use(claims)
        if token_use_result is not None:
            return token_use_result

        audience_result = self._check_audience(claims)
        if audience_result is not None:
            return audience_result

        return AuthResult.success(claims)

    def _check_token_use(self, claims: dict[str, Any]) -> AuthResult | None:
        """Reject tokens whose ``token_use`` is not accepted; else ``None``."""
        token_use = claims.get("token_use")
        if token_use is not None and token_use not in self._config.accepted_token_use:
            return AuthResult.failure(
                AuthError.INVALID_TOKEN_USE,
                f"Unexpected token_use '{token_use}'; "
                f"expected one of {list(self._config.accepted_token_use)}.",
            )
        return None

    def _check_audience(self, claims: dict[str, Any]) -> AuthResult | None:
        """Verify the app client id against ``aud`` (id) or ``client_id`` (access).

        Returns ``None`` when no app client id is configured (audience check
        skipped) or when the audience matches; otherwise an ``AuthResult``
        failure.
        """
        expected = self._config.app_client_id
        if not expected:
            return None

        token_use = claims.get("token_use")
        if token_use == "access":  # nosec B105 - JWT token_use claim value, not a password
            actual: Any = claims.get("client_id")
        else:
            actual = claims.get("aud")

        # `aud` may be a string or a list per the JWT spec.
        matches = actual == expected or (
            isinstance(actual, (list, tuple)) and expected in actual
        )
        if not matches:
            return AuthResult.failure(
                AuthError.INVALID_AUDIENCE,
                "Token audience/client_id does not match the configured app client id.",
            )
        return None


def validate_token(
    raw_token: str | None,
    config: TokenValidatorConfig | None = None,
    jwk_client: Any | None = None,
) -> AuthResult:
    """Convenience wrapper: validate a token using env-derived config.

    Args:
        raw_token: The token string (optionally ``Bearer``-prefixed).
        config: Optional explicit config; defaults to :meth:`from_env`.
        jwk_client: Optional injected JWKS client (used by tests).

    Returns:
        An :class:`AuthResult`. Configuration problems are reported as a
        ``401`` with :attr:`AuthError.CONFIGURATION_ERROR` rather than raising.
    """
    if config is None:
        try:
            config = TokenValidatorConfig.from_env()
        except ValueError as exc:
            return AuthResult.failure(AuthError.CONFIGURATION_ERROR, str(exc))
    return TokenValidator(config, jwk_client=jwk_client).validate(raw_token)


def refresh_access_token(
    refresh_token: str | None,
    config: TokenValidatorConfig | None = None,
    cognito_client: Any | None = None,
) -> RefreshResult:
    """Exchange a valid Cognito refresh token for a new access token.

    Uses the ``REFRESH_TOKEN_AUTH`` flow against ``cognito-idp``. On any
    failure (missing token, invalid/expired refresh token, misconfiguration)
    a :class:`RefreshResult` with ``succeeded=False`` is returned; the
    function does not raise.

    Args:
        refresh_token: The Cognito refresh token.
        config: Optional explicit config; defaults to :meth:`from_env`.
        cognito_client: Optional injected boto3 ``cognito-idp`` client (tests).

    Returns:
        A :class:`RefreshResult` describing the outcome.
    """
    if refresh_token is None or not refresh_token.strip():
        return RefreshResult(
            succeeded=False,
            error=AuthError.MISSING_TOKEN,
            message="No refresh token provided.",
        )

    if config is None:
        try:
            config = TokenValidatorConfig.from_env()
        except ValueError as exc:
            return RefreshResult(
                succeeded=False,
                error=AuthError.CONFIGURATION_ERROR,
                message=str(exc),
            )

    if not config.app_client_id:
        return RefreshResult(
            succeeded=False,
            error=AuthError.CONFIGURATION_ERROR,
            message="COGNITO_APP_CLIENT_ID is required for the refresh flow.",
        )

    client = cognito_client
    if client is None:
        try:
            import boto3  # imported lazily so validation has no boto3 dependency

            client = boto3.client("cognito-idp", region_name=config.region)
        except Exception as exc:  # pragma: no cover - environment dependent
            return RefreshResult(
                succeeded=False,
                error=AuthError.CONFIGURATION_ERROR,
                message=f"Unable to create Cognito client: {exc}",
            )

    try:
        response = client.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            ClientId=config.app_client_id,
            AuthParameters={"REFRESH_TOKEN": refresh_token.strip()},
        )
    except Exception as exc:
        # Covers NotAuthorizedException (expired/invalid refresh token) and
        # any client/network error. Reported cleanly as an auth failure.
        return RefreshResult(
            succeeded=False,
            error=AuthError.EXPIRED_TOKEN,
            message=f"Refresh token exchange failed: {exc}",
        )

    auth = response.get("AuthenticationResult") or {}
    access_token = auth.get("AccessToken")
    if not access_token:
        return RefreshResult(
            succeeded=False,
            error=AuthError.UNKNOWN,
            message="Refresh succeeded but no access token was returned.",
        )

    return RefreshResult(
        succeeded=True,
        access_token=access_token,
        id_token=auth.get("IdToken"),
        expires_in=auth.get("ExpiresIn"),
        token_type=auth.get("TokenType"),
        message="Issued new access token.",
    )


__all__ = [
    "AuthError",
    "AuthResult",
    "RefreshResult",
    "TokenValidator",
    "TokenValidatorConfig",
    "refresh_access_token",
    "validate_token",
]
