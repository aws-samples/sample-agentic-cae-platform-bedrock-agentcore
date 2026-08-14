import { createContext } from 'react'
import type { ClientMessage, ConnectionStatus, ServerMessage } from './types'

/** Value exposed by {@link WebSocketProvider} via {@link useWebSocket}. */
export interface WebSocketContextValue {
  /** Current connection status (drives the status indicator). */
  status: ConnectionStatus
  /** Send a user action; queued automatically while disconnected (Req 8.6). */
  send: (action: ClientMessage['action'], data: unknown) => void
  /** Most recent server message received, or `null` before the first one. */
  lastMessage: ServerMessage | null
  /** Highest sequence delivered so far for the active session. */
  lastSequence: number
  /** Trigger a manual reconnect (for the failure indicator button, Req 12.5). */
  reconnect: () => void
}

export const WebSocketContext = createContext<WebSocketContextValue | null>(null)
