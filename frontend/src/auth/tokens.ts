import { fetchAuthSession } from 'aws-amplify/auth'

/**
 * Token access + session-expiry coordination for the auth layer.
 *
 * This module is intentionally framework-agnostic (no React) so non-component
 * code — notably the WebSocket client (task 12.3) and HTTP/upload calls — can
 * obtain a fresh access token and signal 401s without importing React context.
 */

/**
 * Return the current Cognito access token as a JWT string, or `null` when the
 * user is not authenticated.
 *
 * Amplify manages refresh automatically: when the cached access token is
 * expired but a valid refresh token exists, `fetchAuthSession()` transparently
 * mints a new access token using the refresh token — satisfying Req 9.5 (no
 * re-entry of credentials). Pass `forceRefresh` to proactively rotate the
 * token even if the cached one has not yet expired.
 *
 * @param forceRefresh when true, bypasses the cache and forces Amplify to
 *   exchange the refresh token for a new access token.
 * @returns the access token JWT, or `null` if there is no active session.
 */
export async function getAccessToken(forceRefresh = false): Promise<string | null> {
  try {
    const session = await fetchAuthSession(forceRefresh ? { forceRefresh: true } : undefined)
    return session.tokens?.accessToken?.toString() ?? null
  } catch {
    // No session, or refresh failed (e.g. refresh token expired/revoked).
    return null
  }
}

/** Callback invoked when the platform detects an unrecoverable 401. */
export type AuthExpiredHandler = () => void

const authExpiredHandlers = new Set<AuthExpiredHandler>()

/**
 * Register a handler to run when a 401 is observed by any transport layer
 * (WebSocket handshake rejection, HTTP 401, or a failed token refresh).
 *
 * The `AuthProvider` registers a handler that signs the user out and clears
 * auth state, which in turn causes `RequireAuth` to redirect to `/login`
 * (Req 9.3). The WebSocket/HTTP layers should call {@link notifyAuthExpired}
 * rather than navigating directly, keeping redirect policy in one place.
 *
 * @returns an unsubscribe function.
 */
export function onAuthExpired(handler: AuthExpiredHandler): () => void {
  authExpiredHandlers.add(handler)
  return () => {
    authExpiredHandlers.delete(handler)
  }
}

/**
 * Notify the auth layer that a request was rejected with 401 (missing,
 * malformed, or expired token that could not be refreshed). Transport code
 * (WS/HTTP) calls this on a 401 to trigger sign-out + redirect-to-login.
 */
export function notifyAuthExpired(): void {
  for (const handler of authExpiredHandlers) {
    handler()
  }
}
