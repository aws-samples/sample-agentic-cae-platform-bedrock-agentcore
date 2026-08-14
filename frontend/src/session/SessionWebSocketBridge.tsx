import type { ReactNode } from 'react'
import { WebSocketProvider } from '../ws/WebSocketProvider'
import { getAccessToken } from '../auth/tokens'
import { useSession } from './useSession'

/**
 * Bridges the session layer to the WebSocket layer: it reads the active session
 * id from {@link useSession} and, when one is selected, mounts a
 * {@link WebSocketProvider} scoped to that session (feeding it `getAccessToken`
 * for auth). Remounting on session change gives each session its own connection
 * and message ordering (Req 8.6/8.7).
 *
 * When no session is active yet, it renders children without a provider —
 * panels read the WebSocket context defensively (`useContext(...) === null`),
 * so the app still builds and renders (Req 8.3).
 */
function SessionWebSocketBridge({ children }: { children: ReactNode }) {
  const { activeSessionId } = useSession()

  if (activeSessionId === null) {
    return <>{children}</>
  }

  return (
    <WebSocketProvider key={activeSessionId} sessionId={activeSessionId} getToken={getAccessToken}>
      {children}
    </WebSocketProvider>
  )
}

export default SessionWebSocketBridge
