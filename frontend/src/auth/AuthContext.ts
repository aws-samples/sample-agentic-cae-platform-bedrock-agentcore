import { createContext, useContext } from 'react'

/** Minimal authenticated-user shape surfaced to the UI. */
export interface AuthUser {
  /** Cognito user sub (stable unique id). */
  userId: string
  /** Username (email) the user signed in with. */
  username: string
}

/** Result of a sign-in attempt. */
export interface SignInResult {
  /** True when the user is fully signed in. */
  isSignedIn: boolean
  /**
   * True when Cognito requires email OTP confirmation before sign-in can
   * complete (i.e. the account was created but never verified). The UI should
   * route the user to the verification step.
   */
  needsConfirmation: boolean
}

/** Result of a sign-up attempt. */
export interface SignUpResult {
  /**
   * True when an email OTP confirmation step is required (the normal path).
   * When false, the account is already usable and the user can sign in.
   */
  needsConfirmation: boolean
}

/**
 * Public auth API exposed via {@link useAuth}. Actions wrap the Amplify v6
 * `aws-amplify/auth` functions and keep {@link AuthContextValue.user} /
 * {@link AuthContextValue.isAuthenticated} in sync.
 */
export interface AuthContextValue {
  /** The signed-in user, or `null` when unauthenticated. */
  user: AuthUser | null
  /** Convenience flag: `user !== null`. */
  isAuthenticated: boolean
  /** True while the initial session hydration is in flight. */
  isLoading: boolean

  /** Sign up with email + password. Triggers an email OTP (Req 9.1). */
  signUp: (email: string, password: string) => Promise<SignUpResult>
  /** Confirm sign-up with the emailed OTP code (Req 9.1 / 9.4). */
  confirmSignUp: (email: string, code: string) => Promise<void>
  /** Resend the email OTP code. */
  resendCode: (email: string) => Promise<void>
  /** Sign in with email + password (Req 9.1). */
  signIn: (email: string, password: string) => Promise<SignInResult>
  /** Sign out, invalidating the refresh token (Req 9.4). */
  signOut: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

/**
 * Access the auth API. Must be used within an `<AuthProvider>`.
 *
 * Note: for non-React callers (WebSocket client, HTTP layer) prefer the
 * standalone `getAccessToken()` / `onAuthExpired()` helpers in `./tokens`.
 */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (ctx === null) {
    throw new Error('useAuth must be used within an <AuthProvider>')
  }
  return ctx
}
