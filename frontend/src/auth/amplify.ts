import { Amplify } from 'aws-amplify'
import { cognitoUserPoolsTokenProvider } from 'aws-amplify/auth/cognito'
import { sharedInMemoryStorage } from 'aws-amplify/utils'

/**
 * Amplify Auth (v6 modular API) configuration for the Cognito User Pool.
 *
 * All identifiers are injected at build time via Vite env vars (`VITE_*`) that
 * originate from the CDK stack outputs (see task 2.1). Nothing is hardcoded so
 * the same bundle can target dev/beta/prod by swapping `.env` values.
 *
 *   VITE_COGNITO_USER_POOL_ID   - Cognito User Pool ID   (e.g. us-east-1_ABC123)
 *   VITE_COGNITO_APP_CLIENT_ID  - Cognito App Client ID
 *   VITE_AWS_REGION             - AWS region (optional; derived from pool id)
 *
 * Token storage (Req 9.3 / secure handling): Amplify v6 defaults to persisting
 * Cognito tokens in `window.localStorage`. We override that with Amplify's
 * built-in `sharedInMemoryStorage` KeyValueStorage so access/refresh/id tokens
 * live ONLY in memory for the lifetime of the tab and are never written to
 * localStorage. A full page reload therefore clears the session (the user
 * re-authenticates), which is the intended trade-off for not persisting JWTs.
 */

const userPoolId = import.meta.env.VITE_COGNITO_USER_POOL_ID ?? ''
const userPoolClientId = import.meta.env.VITE_COGNITO_APP_CLIENT_ID ?? ''

/**
 * True only when the real Cognito identifiers are present. The app still builds
 * and renders without them (useful for local UI work / CI builds that have no
 * stack outputs); auth actions simply surface a clear "not configured" error
 * instead of throwing at module load.
 */
export const isAuthConfigured: boolean =
  userPoolId.length > 0 && userPoolClientId.length > 0

let configured = false

/**
 * Configure Amplify exactly once and pin token storage to memory. Safe to call
 * multiple times (e.g. React StrictMode double-invoke) — subsequent calls are
 * no-ops. Returns whether a usable Cognito configuration was applied.
 */
export function configureAuth(): boolean {
  if (configured) {
    return isAuthConfigured
  }
  configured = true

  // Pin Cognito token persistence to in-memory storage regardless of whether
  // real credentials are present, so no code path can fall back to localStorage.
  cognitoUserPoolsTokenProvider.setKeyValueStorage(sharedInMemoryStorage)

  if (!isAuthConfigured) {
    // eslint-disable-next-line no-console
    console.warn(
      '[auth] Cognito is not configured. Set VITE_COGNITO_USER_POOL_ID and ' +
        'VITE_COGNITO_APP_CLIENT_ID (from CDK stack outputs) to enable sign-in.',
    )
    return false
  }

  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId,
        userPoolClientId,
        loginWith: {
          email: true,
        },
      },
    },
  })

  return true
}
