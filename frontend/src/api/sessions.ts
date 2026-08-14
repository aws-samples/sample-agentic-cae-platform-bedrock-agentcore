import { getAccessToken, notifyAuthExpired } from '../auth/tokens'
import { getApiEndpoint } from './upload'
import type { ModelGenerationState, OptimizationHistoryEntry } from '../panels/results/types'

/**
 * HTTP client for Session persistence + restoration (Req 13.2, 13.3, 13.4, 13.6).
 *
 * Sessions are persisted in DynamoDB by the backend and served by the platform
 * HTTP API (Cognito-authorized), NOT the WebSocket gateway. This mirrors the
 * client style in `./upload`:
 *   - base URL resolved from `VITE_API_ENDPOINT` (via {@link getApiEndpoint}),
 *   - the Cognito access token attached as `Authorization: Bearer <jwt>`,
 *   - 401s routed through {@link notifyAuthExpired} so the auth layer performs
 *     the sign-out / login redirect in one place (Req 9.3).
 *
 * Network calls are kept behind the two exported functions ({@link listSessions},
 * {@link getSession}) so they are trivial to stub/mock in tests.
 */

/** Maximum number of sessions the list surfaces (Req 13.2). */
export const MAX_SESSIONS = 50

/**
 * Create a new (empty) session server-side and return its summary. Used to
 * start a fresh conversation; the returned `sessionId` scopes the WebSocket.
 */
export async function createSession(title?: string): Promise<SessionSummary> {
  const response = await fetch(apiUrl('/api/sessions'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
    },
    body: JSON.stringify(title ? { title } : {}),
  })
  await assertOk(response, 'Creating session')
  const raw = (await response.json()) as unknown
  const summary = normalizeSummary(raw)
  if (summary === null) {
    throw new Error('Create session response was missing a sessionId.')
  }
  return summary
}

/**
 * A lightweight session record for the session list (Req 13.6): enough to show
 * a title/summary, the last-access date, and the message count without loading
 * the full conversation.
 */
export interface SessionSummary {
  /** Server-generated unique session identifier. */
  sessionId: string
  /** Human-readable title or summary of the session. */
  title: string
  /** ISO-8601 timestamp of the most recent access (used for ordering). */
  lastAccessAt: string
  /** Number of messages in the conversation. */
  messageCount: number
}

/** A restored drawing associated with a session (opaque to this layer). */
export interface SessionDrawing {
  drawingId: string
  [key: string]: unknown
}

/** A restored chat message associated with a session (opaque to this layer). */
export interface SessionMessage {
  [key: string]: unknown
}

/** A restored optimization result associated with a session (opaque here). */
export interface SessionOptimizationResult {
  [key: string]: unknown
}

/**
 * The full session payload used to rehydrate the app on selection (Req 13.3):
 * drawings, chat history, and optimization results. The nested item shapes are
 * intentionally loose so this layer stays decoupled from panel-specific models
 * — panels read what they need from the restored data.
 */
export interface SessionDetail {
  sessionId: string
  title?: string
  lastAccessAt?: string
  drawings: SessionDrawing[]
  messages: SessionMessage[]
  optimizationResults: SessionOptimizationResult[]
  optimizationHistory: OptimizationHistoryEntry[]
}

/** Join the API base URL with a path, tolerating a trailing slash on the base. */
function apiUrl(path: string): string {
  const base = getApiEndpoint().replace(/\/+$/, '')
  return `${base}${path}`
}

/** Build an `Authorization` header, signalling auth expiry when no token exists. */
async function authHeaders(): Promise<Record<string, string>> {
  const token = await getAccessToken()
  if (token === null) {
    notifyAuthExpired()
    throw new Error('Not authenticated. Please sign in again.')
  }
  return { Authorization: `Bearer ${token}` }
}

/** Throw on a non-OK response, routing 401s through the auth-expiry handler. */
async function assertOk(response: Response, action: string): Promise<void> {
  if (response.ok) {
    return
  }
  if (response.status === 401) {
    notifyAuthExpired()
    throw new Error('Your session has expired. Please sign in again.')
  }
  let detail = ''
  try {
    detail = (await response.text()).slice(0, 500)
  } catch {
    // Ignore body read failures; the status code is enough context.
  }
  throw new Error(`${action} failed (HTTP ${response.status}).${detail ? ` ${detail}` : ''}`)
}

/**
 * Coerce a raw record from the API into a {@link SessionSummary}, tolerating
 * either a `title` or `summary` field and either camelCase or snake_case keys.
 * Returns `null` when the record has no usable session id so callers can skip
 * malformed entries defensively.
 */
export function normalizeSummary(raw: unknown): SessionSummary | null {
  if (typeof raw !== 'object' || raw === null) {
    return null
  }
  const record = raw as Record<string, unknown>
  const sessionId = record.sessionId ?? record.session_id
  if (typeof sessionId !== 'string' || sessionId.length === 0) {
    return null
  }
  const title =
    (typeof record.title === 'string' && record.title) ||
    (typeof record.summary === 'string' && record.summary) ||
    'Untitled session'
  const lastAccessAt =
    (typeof record.lastAccessAt === 'string' && record.lastAccessAt) ||
    (typeof record.last_access_at === 'string' && record.last_access_at) ||
    ''
  const rawCount = record.messageCount ?? record.message_count
  const messageCount = typeof rawCount === 'number' && Number.isFinite(rawCount) ? rawCount : 0
  return { sessionId, title, lastAccessAt, messageCount }
}

/**
 * Fetch the authenticated user's session list, most-recent first, capped at
 * {@link MAX_SESSIONS} (Req 13.2). The server is expected to order + cap the
 * list, but callers should still defensively sort via `sortSessionsByRecency`.
 */
export async function listSessions(): Promise<SessionSummary[]> {
  const response = await fetch(apiUrl('/api/sessions'), {
    method: 'GET',
    headers: { ...(await authHeaders()) },
  })
  await assertOk(response, 'Loading sessions')

  const data = (await response.json()) as { sessions?: unknown }
  const rawSessions = Array.isArray(data.sessions) ? data.sessions : []
  const summaries: SessionSummary[] = []
  for (const raw of rawSessions) {
    const summary = normalizeSummary(raw)
    if (summary !== null) {
      summaries.push(summary)
    }
  }
  return summaries
}

/**
 * Fetch the full detail for a single session so the app can restore its
 * drawings, chat history, and optimization results (Req 13.3). Missing
 * collections default to empty arrays so consumers never see `undefined`.
 */
export async function getSession(sessionId: string): Promise<SessionDetail> {
  const response = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}`), {
    method: 'GET',
    headers: { ...(await authHeaders()) },
  })
  await assertOk(response, 'Loading session')

  const data = (await response.json()) as Partial<SessionDetail> & {
    session_id?: string
    optimization_results?: SessionOptimizationResult[]
    optimization_history?: OptimizationHistoryEntry[]
  }
  return {
    sessionId: data.sessionId ?? data.session_id ?? sessionId,
    title: data.title,
    lastAccessAt: data.lastAccessAt,
    drawings: Array.isArray(data.drawings) ? data.drawings : [],
    messages: Array.isArray(data.messages) ? data.messages : [],
    optimizationResults: Array.isArray(data.optimizationResults)
      ? data.optimizationResults
      : Array.isArray(data.optimization_results)
        ? data.optimization_results
        : [],
    optimizationHistory: Array.isArray(data.optimizationHistory)
      ? data.optimizationHistory
      : Array.isArray(data.optimization_history)
        ? data.optimization_history
        : [],
  }
}


/**
 * Fetch optimization history for a single drawing across ALL sessions
 * (drawing-scoped, not session-scoped). This is the default view for the
 * Design Exploration tab: every optimization run for the active drawing,
 * most recent first. Returns an empty array when the drawing has no runs.
 */
export async function getDrawingHistory(drawingId: string): Promise<OptimizationHistoryEntry[]> {
  const response = await fetch(
    apiUrl(`/api/sessions?historyForDrawing=${encodeURIComponent(drawingId)}`),
    { method: 'GET', headers: { ...(await authHeaders()) } },
  )
  await assertOk(response, 'Loading optimization history')
  const data = (await response.json()) as { optimizationHistory?: OptimizationHistoryEntry[] }
  return Array.isArray(data.optimizationHistory) ? data.optimizationHistory : []
}

/**
 * Fetch the 3D model-generation states for a single drawing across ALL sessions
 * (drawing-scoped, not session-scoped). Used by the Results Dashboard as a
 * transport-reliability fallback: while a design is `generating`, the dashboard
 * polls this endpoint so the lifecycle converges even if the live WebSocket
 * `model_generated` frame is missed (Req 2.2, 4.3). Returns an empty array when
 * the drawing has no model-generation records.
 */
export async function getModelGenerationStates(
  drawingId: string,
): Promise<ModelGenerationState[]> {
  const response = await fetch(
    apiUrl(`/api/sessions?modelsForDrawing=${encodeURIComponent(drawingId)}`),
    { method: 'GET', headers: { ...(await authHeaders()) } },
  )
  await assertOk(response, 'Loading 3D model states')
  const data = (await response.json()) as { modelStates?: ModelGenerationState[] }
  return Array.isArray(data.modelStates) ? data.modelStates : []
}

/**
 * Delete a session by its id. The backend removes the session row from DynamoDB;
 * the client removes it from the local list on success.
 */
export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}`), {
    method: 'DELETE',
    headers: { ...(await authHeaders()) },
  })
  // 204 No Content or 200 OK both count as success; 404 means it's already gone.
  if (response.status === 404 || response.ok) {
    return
  }
  await assertOk(response, 'Deleting session')
}
