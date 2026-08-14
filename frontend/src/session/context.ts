import { createContext } from 'react'
import type { SessionDetail, SessionSummary } from '../api/sessions'

/** Lifecycle of the session-detail load for the active session (Req 13.3/13.4). */
export type SessionLoadState = 'idle' | 'loading' | 'error'

/** Value exposed by {@link SessionProvider} via {@link useSession}. */
export interface SessionContextValue {
  /** Ordered (most-recent-first) session summaries for the list (Req 13.2/13.6). */
  sessions: SessionSummary[]
  /** Load state of the summary list itself. */
  listState: SessionLoadState
  /** Error message for a failed list load, or `null` (Req 13.4). */
  listError: string | null

  /** The currently selected session id, or `null` when none is active. */
  activeSessionId: string | null
  /** Restored detail for the active session, or `null` before/while loading. */
  activeSession: SessionDetail | null
  /** Load state of the active session's detail (Req 13.3/13.4). */
  loadState: SessionLoadState
  /** Error message for a failed detail load, or `null` (Req 13.4). */
  error: string | null

  /** Reload the session summary list (Req 13.2). */
  refreshSessions: () => Promise<void>
  /** Select a session and restore its full detail (Req 13.3). */
  selectSession: (sessionId: string) => Promise<void>
  /** Create a new session and make it active (scopes the WebSocket). */
  createSession: (title?: string) => Promise<void>
  /**
   * Begin a fresh conversation: generates a new client-side session id and
   * makes it active (without a detail fetch), so the WebSocket connects
   * immediately. Used to bootstrap on sign-in and by a "New chat" action.
   */
  startNewSession: () => void
  /**
   * Retry the most recent failed load (list or detail) without changing the
   * user's navigation state (Req 13.4).
   */
  retry: () => Promise<void>
  /** Delete a session by id (removes from backend + local list). */
  deleteSession: (sessionId: string) => Promise<void>
}

export const SessionContext = createContext<SessionContextValue | null>(null)
