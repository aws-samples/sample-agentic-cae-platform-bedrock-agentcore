import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { WebSocketClient } from './WebSocketClient'
import { WebSocketContext } from './context'
import type { WebSocketContextValue } from './context'
import { getWebSocketEndpoint } from './env'
import type { ClientMessage, ConnectionStatus, ServerMessage } from './types'

export interface WebSocketProviderProps {
  /** Active session id used for auth and message routing. */
  sessionId: string
  /**
   * Async Cognito access-token provider from the auth layer (task 12.2), e.g.
   * `getAccessToken`. Passing it in keeps this module decoupled from auth.
   */
  getToken: () => Promise<string | null>
  /** Endpoint override; defaults to `VITE_WEBSOCKET_ENDPOINT`. */
  endpoint?: string
  children: ReactNode
}

/**
 * Instantiates a {@link WebSocketClient} for the session and exposes its status,
 * `send`, latest message, and manual reconnect through context. Connects on
 * mount and cleans up on unmount. When no endpoint is configured, it stays
 * `closed` without connecting so the app still builds and renders (Req 8.3).
 */
export function WebSocketProvider({
  sessionId,
  getToken,
  endpoint,
  children,
}: WebSocketProviderProps) {
  const resolvedEndpoint = endpoint ?? getWebSocketEndpoint()
  const [status, setStatus] = useState<ConnectionStatus>('closed')
  const [lastMessage, setLastMessage] = useState<ServerMessage | null>(null)
  const [lastSequence, setLastSequence] = useState(0)
  const clientRef = useRef<WebSocketClient | null>(null)

  useEffect(() => {
    if (!resolvedEndpoint) {
      return
    }
    const client = new WebSocketClient({
      endpoint: resolvedEndpoint,
      sessionId,
      getToken,
    })
    clientRef.current = client

    const unsubscribeStatus = client.onStatusChange(setStatus)
    const unsubscribeMessage = client.onMessage((message) => {
      setLastMessage(message)
      setLastSequence(client.getLastSequence())
    })

    void client.connect()

    return () => {
      unsubscribeStatus()
      unsubscribeMessage()
      client.close()
      clientRef.current = null
    }
  }, [resolvedEndpoint, sessionId, getToken])

  const value = useMemo<WebSocketContextValue>(
    () => ({
      status,
      lastMessage,
      lastSequence,
      send: (action: ClientMessage['action'], data: unknown) =>
        clientRef.current?.send(action, data),
      reconnect: () => clientRef.current?.reconnect(),
    }),
    [status, lastMessage, lastSequence],
  )

  return (
    <WebSocketContext.Provider value={value}>
      {children}
    </WebSocketContext.Provider>
  )
}

export default WebSocketProvider
