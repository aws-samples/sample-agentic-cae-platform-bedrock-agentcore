import { useContext } from 'react'
import { WebSocketContext } from './context'
import type { WebSocketContextValue } from './context'

/**
 * Access the active WebSocket connection (status, `send`, latest message, and
 * manual reconnect). Must be used within a {@link WebSocketProvider}.
 */
export function useWebSocket(): WebSocketContextValue {
  const value = useContext(WebSocketContext)
  if (value === null) {
    throw new Error('useWebSocket must be used within a WebSocketProvider')
  }
  return value
}
