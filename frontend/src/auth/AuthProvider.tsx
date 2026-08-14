import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  confirmSignUp as amplifyConfirmSignUp,
  fetchUserAttributes,
  getCurrentUser,
  resendSignUpCode,
  signIn as amplifySignIn,
  signOut as amplifySignOut,
  signUp as amplifySignUp,
} from 'aws-amplify/auth'
import { AuthContext } from './AuthContext'
import type {
  AuthContextValue,
  AuthUser,
  SignInResult,
  SignUpResult,
} from './AuthContext'
import { configureAuth } from './amplify'
import { onAuthExpired } from './tokens'

// Configure Amplify + pin token storage to memory before any auth call runs.
configureAuth()

/**
 * Provides authentication state and actions to the React tree.
 *
 * On mount it hydrates any existing in-memory session (survives client-side
 * navigation but not a full reload, by design — see `./amplify`). It also
 * registers a global auth-expired handler so a 401 detected by the WebSocket
 * or HTTP layer clears state and triggers the login redirect (Req 9.3).
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const refreshUser = useCallback(async (): Promise<AuthUser | null> => {
    try {
      const current = await getCurrentUser()
      // Fetch user attributes to get the email/name for display (getCurrentUser
      // only returns userId + username which may be the UUID on email-alias pools).
      let displayName = current.username
      try {
        const attrs = await fetchUserAttributes()
        displayName = attrs.name || attrs.email || current.username
      } catch {
        // Attributes unavailable — fall back to the username from getCurrentUser.
      }
      const next: AuthUser = {
        userId: current.userId,
        username: displayName,
      }
      setUser(next)
      return next
    } catch {
      setUser(null)
      return null
    }
  }, [])

  // Hydrate the session once on mount.
  useEffect(() => {
    let active = true
    void (async () => {
      await refreshUser()
      if (active) {
        setIsLoading(false)
      }
    })()
    return () => {
      active = false
    }
  }, [refreshUser])

  const signOut = useCallback(async (): Promise<void> => {
    try {
      await amplifySignOut()
    } finally {
      // Clear local state even if the network sign-out call fails so the UI
      // always returns to the unauthenticated view.
      setUser(null)
    }
  }, [])

  // Register the 401 / expiry handler: sign out and drop to the login view.
  useEffect(() => {
    const unsubscribe = onAuthExpired(() => {
      void signOut()
    })
    return unsubscribe
  }, [signOut])

  const signUp = useCallback(
    async (email: string, password: string): Promise<SignUpResult> => {
      const result = await amplifySignUp({
        username: email,
        password,
        options: { userAttributes: { email } },
      })
      return { needsConfirmation: !result.isSignUpComplete }
    },
    [],
  )

  const confirmSignUp = useCallback(
    async (email: string, code: string): Promise<void> => {
      await amplifyConfirmSignUp({ username: email, confirmationCode: code })
    },
    [],
  )

  const resendCode = useCallback(async (email: string): Promise<void> => {
    await resendSignUpCode({ username: email })
  }, [])

  const signIn = useCallback(
    async (email: string, password: string): Promise<SignInResult> => {
      const result = await amplifySignIn({ username: email, password })
      if (result.isSignedIn) {
        await refreshUser()
        return { isSignedIn: true, needsConfirmation: false }
      }
      const needsConfirmation = result.nextStep.signInStep === 'CONFIRM_SIGN_UP'
      return { isSignedIn: false, needsConfirmation }
    },
    [refreshUser],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: user !== null,
      isLoading,
      signUp,
      confirmSignUp,
      resendCode,
      signIn,
      signOut,
    }),
    [user, isLoading, signUp, confirmSignUp, resendCode, signIn, signOut],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}
