import { MAX_SESSIONS, type SessionSummary } from '../api/sessions'

/**
 * Pure helpers for the session list. Kept DOM/React-free so they are trivial to
 * unit- and property-test.
 */

/** Parse an ISO timestamp to epoch millis; unparseable values sort as oldest. */
function accessTime(session: SessionSummary): number {
  const ms = Date.parse(session.lastAccessAt)
  return Number.isNaN(ms) ? Number.NEGATIVE_INFINITY : ms
}

/**
 * Order sessions by last-access timestamp, most recent first, and cap the
 * result at {@link MAX_SESSIONS} (Req 13.2). The backend is expected to already
 * order + cap, but applying this client-side keeps the UI correct regardless of
 * server behavior. Does not mutate the input array.
 */
export function sortSessionsByRecency(sessions: SessionSummary[]): SessionSummary[] {
  return [...sessions].sort((a, b) => accessTime(b) - accessTime(a)).slice(0, MAX_SESSIONS)
}

/**
 * Format an ISO last-access timestamp for display in the session list
 * (Req 13.6). Falls back to a placeholder for missing/invalid values so the row
 * still renders. Uses the locale date + short time.
 */
export function formatLastAccess(lastAccessAt: string): string {
  const ms = Date.parse(lastAccessAt)
  if (Number.isNaN(ms)) {
    return 'Unknown'
  }
  const date = new Date(ms)
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * Format a message count into a short, pluralized label for the session list
 * (Req 13.6).
 */
export function formatMessageCount(count: number): string {
  const safe = Number.isFinite(count) && count > 0 ? Math.floor(count) : 0
  return `${safe} ${safe === 1 ? 'message' : 'messages'}`
}

/**
 * Generate a creative session title inspired by engineering CAE sessions
 * nomenclature — e.g. "Session #4 — Jul 6" or "Analysis #12 — Jun 28".
 *
 * This makes new sessions feel purposeful and identifiable (like corporate chat
 * apps naming threads by topic/date) without requiring the user to title them
 * manually upfront. The title updates to the first question's content once the
 * user actually asks something (handled by the chat-persistence layer), but
 * this gives a distinctive initial identity.
 */
export function generateSessionTitle(_existingCount: number): string {
  // A neutral placeholder shown only until the user asks their first question,
  // at which point the backend (chat-persistence layer) replaces the title
  // with the truncated first question so the sidebar lists conversations by
  // their opening topic (e.g. "Optimize the heat sink for minimum weight…").
  return 'New Session'
}
