import { DEFAULT_BACKOFF_SCHEDULE } from './backoff'
import type {
  ClientMessage,
  ConnectionStatus,
  ServerMessage,
  TimerHandle,
  WebSocketClientConfig,
  WebSocketLike,
} from './types'

/** WebSocket `readyState` value for an open connection. */
const WS_OPEN = 1

/** Normal closure code — a clean, intentional close that must not reconnect. */
const WS_NORMAL_CLOSURE = 1000

type MessageListener = (message: ServerMessage) => void
type StatusListener = (status: ConnectionStatus) => void

/**
 * Framework-agnostic WebSocket connection manager with reconnection.
 *
 * Responsibilities (Req 8.3, 8.6, 12.4, 12.5):
 * - Connect using a Cognito access token passed as a `token` query param
 *   (browsers cannot set handshake headers) plus a `session_id` param.
 * - Queue outbound user actions while disconnected and flush them in order on
 *   (re)connect (Req 8.6).
 * - Reconnect with a fixed exponential backoff schedule (1, 2, 4, 8, 15s — max
 *   5 attempts within ~30s). On success, request messages missed during the
 *   disconnection for the active session (Req 12.4).
 * - Transition to `failed` when all attempts are exhausted so the UI can show
 *   an error and a manual reconnect action (Req 12.5).
 *
 * Incoming messages are delivered to subscribers in `sequence` order, and the
 * highest observed sequence is tracked as the resync anchor.
 *
 * ## Resync convention
 * On a successful reconnect the client sends:
 * `{ action: "sendmessage", session_id, data: { type: "resync", last_sequence } }`
 * where `last_sequence` is the highest sequence delivered before the drop. The
 * backend replays any messages with a greater sequence for the active session.
 */
export class WebSocketClient {
  private readonly endpoint: string
  private readonly sessionId: string
  private readonly getToken: () => Promise<string | null>
  private readonly socketFactory: (url: string) => WebSocketLike
  private readonly setTimeoutFn: (cb: () => void, ms: number) => TimerHandle
  private readonly clearTimeoutFn: (handle: TimerHandle) => void
  private readonly schedule: readonly number[]
  private readonly maxAttempts: number

  private socket: WebSocketLike | null = null
  private status: ConnectionStatus = 'closed'
  private attempts = 0
  private reconnectTimer: TimerHandle | null = null
  private closedByClient = false
  private hasEverConnected = false

  private readonly queue: ClientMessage[] = []
  private readonly messageListeners = new Set<MessageListener>()
  private readonly statusListeners = new Set<StatusListener>()

  /** Ordered-delivery buffer keyed by sequence, plus the next expected key. */
  private readonly pending = new Map<number, ServerMessage>()
  private nextExpected: number | null = null
  /** Highest sequence delivered for the active session (resync anchor). */
  private highestSequence = 0

  constructor(config: WebSocketClientConfig) {
    this.endpoint = config.endpoint
    this.sessionId = config.sessionId
    this.getToken = config.getToken
    this.socketFactory =
      config.socketFactory ??
      ((url: string) => new WebSocket(url) as unknown as WebSocketLike)
    this.setTimeoutFn =
      config.setTimeoutFn ??
      ((cb: () => void, ms: number) => setTimeout(cb, ms) as TimerHandle)
    this.clearTimeoutFn =
      config.clearTimeoutFn ??
      ((handle: TimerHandle) => clearTimeout(handle as ReturnType<typeof setTimeout>))
    this.schedule = config.backoffSchedule ?? DEFAULT_BACKOFF_SCHEDULE
    this.maxAttempts = this.schedule.length
  }

  /** Current connection status. */
  getStatus(): ConnectionStatus {
    return this.status
  }

  /** Highest sequence delivered so far (the resync anchor / `last_sequence`). */
  getLastSequence(): number {
    return this.highestSequence
  }

  /** Number of queued outbound actions awaiting a connection. */
  getQueueLength(): number {
    return this.queue.length
  }

  /** Subscribe to inbound server messages. Returns an unsubscribe function. */
  onMessage(listener: MessageListener): () => void {
    this.messageListeners.add(listener)
    return () => this.messageListeners.delete(listener)
  }

  /** Subscribe to connection status changes. Returns an unsubscribe function. */
  onStatusChange(listener: StatusListener): () => void {
    this.statusListeners.add(listener)
    return () => this.statusListeners.delete(listener)
  }

  /**
   * Open the connection. Fetches a fresh token, builds the authenticated URL,
   * and wires the socket handlers. Safe to call for both the initial connect
   * and reconnection attempts.
   */
  async connect(): Promise<void> {
    this.closedByClient = false
    this.setStatus(this.attempts === 0 ? 'connecting' : 'reconnecting')

    const token = await this.getToken()
    // A concurrent close()/reconnect() may have run while awaiting the token.
    if (this.closedByClient) {
      return
    }

    const url = this.buildUrl(token)
    const socket = this.socketFactory(url)
    this.socket = socket

    socket.onopen = () => this.handleOpen()
    socket.onmessage = (event) => this.handleMessage(event.data)
    socket.onclose = (event) => this.handleClose(event?.code)
    socket.onerror = () => {
      // Errors are followed by a close event, which drives reconnection.
    }
  }

  /**
   * Send a user action. Delivered immediately when connected, otherwise queued
   * and flushed in order on the next (re)connect (Req 8.6).
   */
  send(action: ClientMessage['action'], data: unknown): void {
    const message: ClientMessage = { action, session_id: this.sessionId, data }
    if (this.socket && this.socket.readyState === WS_OPEN) {
      this.socket.send(JSON.stringify(message))
    } else {
      this.queue.push(message)
    }
  }

  /**
   * Manually restart the connection. Intended for the "reconnect" button shown
   * after all automatic attempts fail (Req 12.5). Resets the attempt counter.
   */
  reconnect(): void {
    this.clearReconnectTimer()
    this.attempts = 0
    void this.connect()
  }

  /** Cleanly close the connection and stop reconnecting. */
  close(): void {
    this.closedByClient = true
    this.clearReconnectTimer()
    if (this.socket) {
      this.socket.close(WS_NORMAL_CLOSURE)
      this.socket = null
    }
    this.setStatus('closed')
  }

  private buildUrl(token: string | null): string {
    const params = new URLSearchParams()
    params.set('token', token ?? '')
    params.set('session_id', this.sessionId)
    const separator = this.endpoint.includes('?') ? '&' : '?'
    return `${this.endpoint}${separator}${params.toString()}`
  }

  private handleOpen(): void {
    const isReconnect = this.hasEverConnected
    this.hasEverConnected = true
    this.attempts = 0
    this.clearReconnectTimer()
    this.setStatus('open')

    // On reconnect, request messages missed during the drop before replaying
    // any queued user actions (Req 12.4).
    if (isReconnect) {
      this.requestMissedMessages()
    }
    this.flushQueue()
  }

  private requestMissedMessages(): void {
    this.send('sendmessage', {
      type: 'resync',
      last_sequence: this.highestSequence,
    })
  }

  private flushQueue(): void {
    while (
      this.queue.length > 0 &&
      this.socket &&
      this.socket.readyState === WS_OPEN
    ) {
      const message = this.queue.shift()
      if (message) {
        this.socket.send(JSON.stringify(message))
      }
    }
  }

  private handleMessage(raw: unknown): void {
    if (typeof raw !== 'string') {
      return
    }
    let message: ServerMessage
    try {
      message = JSON.parse(raw) as ServerMessage
    } catch {
      return
    }
    if (typeof message?.sequence !== 'number') {
      return
    }

    // Buffer by sequence and deliver contiguous runs in order so timing jitter
    // never reorders per-session messages (design Property 18).
    if (this.nextExpected === null) {
      this.nextExpected = message.sequence
    }
    this.pending.set(message.sequence, message)

    while (this.nextExpected !== null && this.pending.has(this.nextExpected)) {
      const next = this.pending.get(this.nextExpected)!
      this.pending.delete(this.nextExpected)
      this.nextExpected += 1
      if (next.session_id === this.sessionId) {
        this.highestSequence = Math.max(this.highestSequence, next.sequence)
      }
      this.dispatchMessage(next)
    }
  }

  private dispatchMessage(message: ServerMessage): void {
    for (const listener of this.messageListeners) {
      listener(message)
    }
  }

  private handleClose(code?: number): void {
    this.socket = null
    if (this.closedByClient || code === WS_NORMAL_CLOSURE) {
      this.setStatus('closed')
      return
    }
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    if (this.attempts >= this.maxAttempts) {
      this.setStatus('failed')
      return
    }
    const delay = this.schedule[this.attempts]
    this.attempts += 1
    this.setStatus('reconnecting')
    this.reconnectTimer = this.setTimeoutFn(() => {
      this.reconnectTimer = null
      void this.connect()
    }, delay)
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      this.clearTimeoutFn(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) {
      return
    }
    this.status = status
    for (const listener of this.statusListeners) {
      listener(status)
    }
  }
}
