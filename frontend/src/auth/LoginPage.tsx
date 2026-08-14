import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from './AuthContext'
import { isAuthConfigured } from './amplify'
import {
  PASSWORD_POLICY_HINT,
  isPasswordValid,
  validatePassword,
} from './password'

/** The three authentication modes surfaced by this unauthenticated gate. */
type Mode = 'signin' | 'signup' | 'verify'

/** Extract a human-readable message from an unknown thrown error. */
function errorMessage(err: unknown): string {
  if (err instanceof Error && err.message) {
    return err.message
  }
  return 'Something went wrong. Please try again.'
}

/**
 * Unauthenticated gate rendered at `/login` (Req 9.1, 9.3, 9.4).
 *
 * Provides sign-in, sign-up (email + policy-checked password), and email OTP
 * verification in a single component with mode switching. On successful
 * sign-in the user is returned to the location they were redirected from
 * (captured in router state by {@link RequireAuth}), defaulting to
 * `/workspace`.
 */
function LoginPage() {
  const { isAuthenticated, signIn, signUp, confirmSignUp, resendCode } =
    useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const redirectTo = useMemo<string>(() => {
    const state = location.state as { from?: { pathname?: string } } | null
    return state?.from?.pathname ?? '/workspace'
  }, [location.state])

  const [mode, setMode] = useState<Mode>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // If a session is already present (e.g. client-side nav to /login), bounce
  // straight to the app.
  useEffect(() => {
    if (isAuthenticated) {
      navigate(redirectTo, { replace: true })
    }
  }, [isAuthenticated, navigate, redirectTo])

  function switchMode(next: Mode) {
    setMode(next)
    setError(null)
    setInfo(null)
  }

  async function handleSignIn(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setInfo(null)
    setSubmitting(true)
    try {
      const result = await signIn(email, password)
      if (result.isSignedIn) {
        navigate(redirectTo, { replace: true })
      } else if (result.needsConfirmation) {
        setMode('verify')
        setInfo('Please confirm your email with the code we sent you.')
      } else {
        setError('Additional sign-in steps are required for this account.')
      }
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleSignUp(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setInfo(null)
    const pwErrors = validatePassword(password)
    if (pwErrors.length > 0) {
      setError(pwErrors.join(' '))
      return
    }
    setSubmitting(true)
    try {
      const result = await signUp(email, password)
      if (result.needsConfirmation) {
        setMode('verify')
        setInfo('We emailed you a verification code. Enter it below.')
      } else {
        setMode('signin')
        setInfo('Account created. You can sign in now.')
      }
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleVerify(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setInfo(null)
    setSubmitting(true)
    try {
      await confirmSignUp(email, code)
      setMode('signin')
      setCode('')
      setInfo('Email verified. Please sign in.')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleResend() {
    setError(null)
    setInfo(null)
    setSubmitting(true)
    try {
      await resendCode(email)
      setInfo('A new verification code has been sent.')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  const inputClasses =
    'w-full rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500'
  const primaryButtonClasses =
    'w-full rounded-md bg-sky-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 disabled:cursor-not-allowed disabled:opacity-60'
  const linkButtonClasses =
    'font-medium text-sky-600 hover:text-sky-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500'

  const title =
    mode === 'signin'
      ? 'Sign in'
      : mode === 'signup'
        ? 'Create your account'
        : 'Verify your email'

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-slate-100 px-4 py-8 text-slate-900">
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-slate-50 p-6 shadow-sm">
        <div className="mb-6 text-center">
          <h1 className="text-lg font-semibold tracking-tight">
            Agentic CAE Platform
          </h1>
          <p className="mt-1 text-sm text-slate-500">{title}</p>
        </div>

        {!isAuthConfigured && (
          <div
            role="status"
            className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
          >
            Authentication is not configured for this build. Set
            <code className="mx-1">VITE_COGNITO_USER_POOL_ID</code> and
            <code className="mx-1">VITE_COGNITO_APP_CLIENT_ID</code> to enable
            sign-in.
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          >
            {error}
          </div>
        )}

        {info && (
          <div
            role="status"
            className="mb-4 rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-800"
          >
            {info}
          </div>
        )}

        {mode === 'signin' && (
          <form className="space-y-4" onSubmit={handleSignIn}>
            <div className="space-y-1">
              <label htmlFor="email" className="text-sm font-medium">
                Email
              </label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                required
                className={inputClasses}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="password" className="text-sm font-medium">
                Password
              </label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                className={inputClasses}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <button
              type="submit"
              className={primaryButtonClasses}
              disabled={submitting || !isAuthConfigured}
            >
              {submitting ? 'Signing in…' : 'Sign in'}
            </button>
            <p className="text-center text-sm text-slate-500">
              Need an account?{' '}
              <button
                type="button"
                className={linkButtonClasses}
                onClick={() => switchMode('signup')}
              >
                Sign up
              </button>
            </p>
          </form>
        )}

        {mode === 'signup' && (
          <form className="space-y-4" onSubmit={handleSignUp}>
            <div className="space-y-1">
              <label htmlFor="signup-email" className="text-sm font-medium">
                Email
              </label>
              <input
                id="signup-email"
                type="email"
                autoComplete="email"
                required
                className={inputClasses}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="signup-password" className="text-sm font-medium">
                Password
              </label>
              <input
                id="signup-password"
                type="password"
                autoComplete="new-password"
                required
                aria-describedby="password-hint"
                className={inputClasses}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <p
                id="password-hint"
                className={
                  password.length > 0 && !isPasswordValid(password)
                    ? 'text-xs text-red-600'
                    : 'text-xs text-slate-500'
                }
              >
                {PASSWORD_POLICY_HINT}
              </p>
            </div>
            <button
              type="submit"
              className={primaryButtonClasses}
              disabled={submitting || !isAuthConfigured}
            >
              {submitting ? 'Creating account…' : 'Create account'}
            </button>
            <p className="text-center text-sm text-slate-500">
              Already have an account?{' '}
              <button
                type="button"
                className={linkButtonClasses}
                onClick={() => switchMode('signin')}
              >
                Sign in
              </button>
            </p>
          </form>
        )}

        {mode === 'verify' && (
          <form className="space-y-4" onSubmit={handleVerify}>
            <div className="space-y-1">
              <label htmlFor="code" className="text-sm font-medium">
                Verification code
              </label>
              <input
                id="code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                className={inputClasses}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
              <p className="text-xs text-slate-500">
                Enter the code sent to {email || 'your email'}. It is valid for
                10 minutes.
              </p>
            </div>
            <button
              type="submit"
              className={primaryButtonClasses}
              disabled={submitting || !isAuthConfigured}
            >
              {submitting ? 'Verifying…' : 'Verify email'}
            </button>
            <p className="text-center text-sm text-slate-500">
              Didn&apos;t get a code?{' '}
              <button
                type="button"
                className={linkButtonClasses}
                onClick={() => void handleResend()}
                disabled={submitting}
              >
                Resend code
              </button>
            </p>
          </form>
        )}
      </div>
    </div>
  )
}

export default LoginPage
