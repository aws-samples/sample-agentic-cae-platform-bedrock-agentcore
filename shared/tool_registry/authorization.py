"""Per-tool authorization checks for the Unified Agent (tool) Registry.

Before *every* tool invocation, the registry verifies that the authenticated
user may operate on the drawing/resource the tool references. Unauthorized
requests are rejected with a structured authorization error and the tool is
**not** executed (Requirement 7.3, design Property 12).

The access check is expressed through an injected *access oracle* so this
module stays pure and unit-testable (mock-free). The oracle may be:

    * a **callable** ``(user_id, resource_id) -> bool``;
    * a **mapping** ``{user_id: <collection of accessible resource ids>}``;
    * a **collection** (set/list/tuple/frozenset) of resource ids already
      scoped to the current user.

Tools that do not reference a specific drawing (``resource_id is None``) have
no resource to guard and are authorized by default.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from .errors import ToolError, authorization_error

# Type alias describing the accepted access-oracle shapes.
AccessOracle = (
    Callable[[str, str], bool]
    | Mapping[str, Collection[str]]
    | Collection[str]
)


@dataclass(frozen=True)
class AuthorizationResult:
    """Outcome of an authorization check performed before a tool runs.

    Attributes:
        authorized: ``True`` only when the user may operate on the resource.
        error: The structured :class:`ToolError` (type ``authorization``) when
            denied; ``None`` when authorized.
    """

    authorized: bool
    error: ToolError | None = None

    @property
    def should_execute(self) -> bool:
        """Whether the caller may proceed to invoke the tool (Req 7.3)."""
        return self.authorized

    @classmethod
    def allow(cls) -> AuthorizationResult:
        return cls(authorized=True, error=None)

    @classmethod
    def deny(cls, error: ToolError) -> AuthorizationResult:
        return cls(authorized=False, error=error)


def _has_access(access: AccessOracle, user_id: str, resource_id: str) -> bool:
    """Resolve whether ``user_id`` may access ``resource_id`` via ``access``.

    Supports the three oracle shapes documented on :data:`AccessOracle`. A
    ``str`` (which is itself a ``Collection``) is treated as a single-resource
    allowlist by exact match, not by substring membership.
    """
    # Callable oracle: delegate the decision.
    if callable(access):
        return bool(access(user_id, resource_id))

    # Per-user mapping: look up the user's accessible resources.
    if isinstance(access, Mapping):
        allowed = access.get(user_id)
        if allowed is None:
            return False
        if isinstance(allowed, str):
            return resource_id == allowed
        return resource_id in allowed

    # A bare string allowlist means "exactly this resource id".
    if isinstance(access, str):
        return resource_id == access

    # Any other collection is a set of resource ids scoped to the user.
    if isinstance(access, Collection):
        return resource_id in access

    raise TypeError(
        "access must be a callable, a mapping of user->resources, or a "
        f"collection of resource ids; got {type(access)!r}"
    )


def authorize_tool_invocation(
    user_id: str,
    tool_name: str,
    resource_id: str | None,
    access: AccessOracle,
) -> AuthorizationResult:
    """Authorize a single tool invocation *before* it executes (Req 7.3).

    Args:
        user_id: The authenticated user id.
        tool_name: The tool about to be invoked.
        resource_id: The drawing/resource the tool references, or ``None`` if
            the tool does not operate on a specific drawing.
        access: The access oracle (see :data:`AccessOracle`).

    Returns:
        :meth:`AuthorizationResult.allow` when access is granted (or no
        resource is referenced). Otherwise :meth:`AuthorizationResult.deny`
        carrying a structured authorization :class:`ToolError` with the
        ``tool_name``, denied ``resource_id``, and a ``denial_reason``. When
        denied, the tool MUST NOT be executed.
    """
    # No referenced drawing -> nothing to guard -> allow.
    if resource_id is None:
        return AuthorizationResult.allow()

    if not user_id or not str(user_id).strip():
        return AuthorizationResult.deny(
            authorization_error(
                tool_name=tool_name,
                resource_id=resource_id,
                denial_reason="No authenticated user; cannot authorize resource access.",
            )
        )

    try:
        granted = _has_access(access, user_id, resource_id)
    except TypeError:
        # A misconfigured oracle is treated as a denial (fail closed) rather
        # than allowing an unverified invocation to proceed.
        return AuthorizationResult.deny(
            authorization_error(
                tool_name=tool_name,
                resource_id=resource_id,
                denial_reason="Access could not be verified for the referenced resource.",
            )
        )

    if granted:
        return AuthorizationResult.allow()

    return AuthorizationResult.deny(
        authorization_error(
            tool_name=tool_name,
            resource_id=resource_id,
            denial_reason=(
                f"User '{user_id}' does not have access to resource '{resource_id}'."
            ),
        )
    )


__all__ = [
    "AccessOracle",
    "AuthorizationResult",
    "authorize_tool_invocation",
]
