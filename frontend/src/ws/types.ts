/**
 * Shared WebSocket protocol + client types.
 *
 * The message shapes mirror the backend WebSocket Gateway (design §9) and the
 * `$connect` / `sendmessage` Lambda routes implemented in tasks 10.1 / 10.2.
 */

/** Client → Server frame body (JSON-stringified over the socket). */
export interface ClientMessage {
  action: 'sendmessage' | 'upload_complete' | 'optimize'
  session_id: string
  data: unknown
}

/** Server → Client frame. `sequence` is a strictly monotonic per-session key. */
export interface ServerMessage {
  type: 'token' | 'progress' | 'stage' | 'result' | 'error'
  session_id: string
  payload: unknown
  timestamp: string
  sequence: number
}

/** Connection lifecycle status surfaced to the UI. */
export type ConnectionStatus =
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'failed'
  | 'closed'

/**
 * Minimal structural interface for the browser `WebSocket`. Kept intentionally
 * small so the client can be driven by a fake socket in tests without a real
 * network connection.
 */
export interface WebSocketLike {
  readyState: number
  send(data: string): void
  close(code?: number, reason?: string): void
  onopen: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose:
    | ((event: { code?: number; reason?: string; wasClean?: boolean }) => void)
    | null
  onerror: ((event: unknown) => void) | null
}

/** Opaque timer handle so injected clocks can use any handle type. */
export type TimerHandle = unknown

/**
 * Configuration for {@link WebSocketClient}.
 *
 * The `getToken` seam decouples this client from the auth layer (task 12.2):
 * the wiring component passes an async token provider (e.g. `getAccessToken`)
 * rather than the client importing the auth module directly. The `socket*`,
 * `setTimeoutFn`, and `clearTimeoutFn` seams make the socket and reconnection
 * timing fully injectable for deterministic tests.
 */
export interface WebSocketClientConfig {
  /** Base WebSocket endpoint (CDK stack output via `VITE_WEBSOCKET_ENDPOINT`). */
  endpoint: string
  /** Active session id, sent as a `session_id` query param and message field. */
  sessionId: string
  /** Async provider for the Cognito access token (from the auth layer, 12.2). */
  getToken: () => Promise<string | null>
  /** Factory for the underlying socket. Defaults to the global `WebSocket`. */
  socketFactory?: (url: string) => WebSocketLike
  /** Timer scheduler. Defaults to the global `setTimeout`. */
  setTimeoutFn?: (callback: () => void, delayMs: number) => TimerHandle
  /** Timer canceller. Defaults to the global `clearTimeout`. */
  clearTimeoutFn?: (handle: TimerHandle) => void
  /** Backoff schedule (ms) per attempt. Defaults to the design schedule. */
  backoffSchedule?: readonly number[]
}
