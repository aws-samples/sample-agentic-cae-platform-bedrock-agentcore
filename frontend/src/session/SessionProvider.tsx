import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  createSession as apiCreateSession,
  deleteSession as apiDeleteSession,
  getSession,
  listSessions,
  type SessionDetail,
  type SessionSummary,
} from '../api/sessions'
import { useAuth } from '../auth/AuthContext'
import { SessionContext } from './context'
import type { SessionContextValue, SessionLoadState } from './context'
import { generateSessionTitle, sortSessionsByRecency } from './helpers'

/**
 * Provides session persistence + restoration to the React tree
 * (Req 13.2, 13.3, 13.4, 13.6).
 *
 * Responsibilities:
 *   - On authentication, load the session summary list ordered by last-access
 *     (most recent first, ≤50) so the user can pick up where they left off
 *     (Req 13.2). The list is re-sorted/capped client-side defensively.
 *   - `selectSession(id)` loads the full session detail — drawings, chat
 *     history, and optimization results — and exposes it via `activeSession`
 *     so panels can rehydrate (Req 13.3).
 *   - Load failures set an error message and keep a retry seam via `retry()`,
 *     without changing the user's route/navigation (Req 13.4).
 *
 * The provider owns `activeSessionId`, which the app root feeds to the
 * WebSocketProvider so realtime traffic is scoped to the active session.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth()

  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [listState, setListState] = useState<SessionLoadState>('idle')
  const [listError, setListError] = useState<string | null>(null)

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [activeSession, setActiveSession] = useState<SessionDetail | null>(null)
  const [loadState, setLoadState] = useState<SessionLoadState>('idle')
  const [error, setError] = useState<string | null>(null)

  // Remember the last failed operation so `retry()` can replay it (Req 13.4).
  const lastFailedRef = useRef<{ kind: 'list' } | { kind: 'detail'; sessionId: string } | null>(
    null,
  )

  const refreshSessions = useCallback(async (): Promise<void> => {
    setListState('loading')
    setListError(null)
    try {
      const list = await listSessions()
      setSessions(sortSessionsByRecency(list))
      setListState('idle')
      lastFailedRef.current = null
    } catch (err) {
      setListState('error')
      setListError(err instanceof Error ? err.message : 'Failed to load sessions.')
      lastFailedRef.current = { kind: 'list' }
    }
  }, [])

  // Latest active id as a ref, so effects can read it without re-subscribing.
  const activeIdRef = useRef<string | null>(null)
  activeIdRef.current = activeSessionId

  /**
   * Start a brand-new conversation. Generates a client-side session id and
   * makes it active without a `/api/sessions/{id}` detail fetch, so the
   * WebSocket connects right away even though the session-list backend
   * (`/api/sessions`) is not yet available.
   */
  const startNewSession = useCallback((): void => {
    const id =
      typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `sess-${Date.now()}-${Math.random().toString(36).slice(2)}`
    setActiveSession(null)
    setLoadState('idle')
    setError(null)
    setActiveSessionId(id)
  }, [])

  const selectSession = useCallback(async (sessionId: string): Promise<void> => {
    // Set the active id immediately so the WebSocket scope + UI update even
    // while the detail is still loading; keep navigation state untouched.
    setActiveSessionId(sessionId)
    setLoadState('loading')
    setError(null)
    try {
      const detail = await getSession(sessionId)
      setActiveSession(detail)
      setLoadState('idle')
      lastFailedRef.current = null
    } catch (err) {
      setActiveSession(null)
      setLoadState('error')
      setError(err instanceof Error ? err.message : 'Failed to load the selected session.')
      lastFailedRef.current = { kind: 'detail', sessionId }
    }
  }, [])

  const createSession = useCallback(async (title?: string): Promise<void> => {
    setLoadState('loading')
    setError(null)
    const sessionTitle = title || generateSessionTitle(sessions.length)
    try {
      const summary = await apiCreateSession(sessionTitle)
      setSessions((prev) => sortSessionsByRecency([summary, ...prev]))
      setActiveSessionId(summary.sessionId)
      // Note: drawings are user-global (not session-scoped), so a new session
      // inherits the same drawings. However we don't have them here — the
      // session detail load will populate them. Set activeSession to null so
      // the workspace doesn't overwrite its cached globalDrawings with [].
      setActiveSession(null)
      setLoadState('idle')
      lastFailedRef.current = null
    } catch (err) {
      setLoadState('error')
      setError(err instanceof Error ? err.message : 'Failed to create a session.')
    }
  }, [sessions.length])

  const deleteSession = useCallback(async (sessionId: string): Promise<void> => {
    try {
      await apiDeleteSession(sessionId)
      setSessions((prev) => prev.filter((s) => s.sessionId !== sessionId))
      // If the deleted session was active, switch to the most recent remaining
      // or start a fresh one.
      if (activeIdRef.current === sessionId) {
        const remaining = sessions.filter((s) => s.sessionId !== sessionId)
        if (remaining.length > 0) {
          await selectSession(remaining[0].sessionId)
        } else {
          startNewSession()
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete session.')
    }
  }, [sessions, selectSession, startNewSession])

  const retry = useCallback(async (): Promise<void> => {
    const last = lastFailedRef.current
    if (last === null) {
      return
    }
    if (last.kind === 'list') {
      await refreshSessions()
    } else {
      await selectSession(last.sessionId)
    }
  }, [refreshSessions, selectSession])

  // Load the session list when the user becomes authenticated; clear it on
  // sign-out so a different user never sees stale sessions (Req 13.2).
  useEffect(() => {
    if (!isAuthenticated) {
      setSessions([])
      setListState('idle')
      setListError(null)
      setActiveSessionId(null)
      setActiveSession(null)
      setLoadState('idle')
      setError(null)
      lastFailedRef.current = null
      return
    }

    let cancelled = false
    setListState('loading')
    setListError(null)
    void (async () => {
      try {
        const list = await listSessions()
        if (cancelled) {
          return
        }
        const sorted = sortSessionsByRecency(list)
        setSessions(sorted)
        setListState('idle')
        lastFailedRef.current = null
        // Bootstrap the active session against the real backend so history and
        // drawings persist and reload: resume the most recent session, or
        // create a fresh server-backed one when the user has none (Req 13.2).
        if (activeIdRef.current === null) {
          if (sorted.length > 0) {
            await selectSession(sorted[0].sessionId)
          } else {
            await createSession()
          }
        }
      } catch (err) {
        if (cancelled) {
          return
        }
        setListState('error')
        setListError(err instanceof Error ? err.message : 'Failed to load sessions.')
        lastFailedRef.current = { kind: 'list' }
        // Keep chat usable even if the sessions backend is unreachable.
        if (activeIdRef.current === null) {
          startNewSession()
        }
      }
    })()

    return () => {
      cancelled = true
    }
  }, [isAuthenticated, selectSession, createSession, startNewSession])

  const value = useMemo<SessionContextValue>(
    () => ({
      sessions,
      listState,
      listError,
      activeSessionId,
      activeSession,
      loadState,
      error,
      refreshSessions,
      selectSession,
      createSession,
      startNewSession,
      deleteSession,
      retry,
    }),
    [
      sessions,
      listState,
      listError,
      activeSessionId,
      activeSession,
      loadState,
      error,
      refreshSessions,
      selectSession,
      createSession,
      startNewSession,
      deleteSession,
      retry,
    ],
  )

  return <SessionContext value={value}>{children}</SessionContext>
}

export default SessionProvider
