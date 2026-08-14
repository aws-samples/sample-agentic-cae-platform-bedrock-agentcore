import { useContext } from 'react'
import { WebSocketContext } from './context'

/**
 * Connection status indicator for the app header.
 *
 * Renders nothing while the connection is healthy (`open`/`connecting`/`closed`)
 * and surfaces a badge when the socket is `reconnecting` or has `failed`. On
 * failure it also offers a manual reconnect action (Req 8.6, 12.5).
 *
 * Reads the WebSocket context directly and degrades to `null` when no
 * {@link WebSocketProvider} is mounted, so it can live in the header before the
 * session/auth wiring (task 12.2) is in place without crashing.
 */
function ConnectionStatus() {
  const ws = useContext(WebSocketContext)

  if (ws === null) {
    return null
  }

  if (ws.status === 'reconnecting') {
    return (
      <span
        role="status"
        className="inline-flex items-center gap-2 rounded-md bg-amber-100 px-3 py-1.5 text-sm font-medium text-amber-800"
      >
        <span
          aria-hidden="true"
          className="h-2 w-2 animate-pulse rounded-full bg-amber-500"
        />
        Reconnecting…
      </span>
    )
  }

  if (ws.status === 'failed') {
    return (
      <span
        role="alert"
        className="inline-flex items-center gap-2 rounded-md bg-red-100 px-3 py-1.5 text-sm font-medium text-red-800"
      >
        <span aria-hidden="true" className="h-2 w-2 rounded-full bg-red-500" />
        Connection lost
        <button
          type="button"
          onClick={ws.reconnect}
          className="rounded bg-red-600 px-2 py-1 text-xs font-semibold text-white transition-colors hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500"
        >
          Reconnect
        </button>
      </span>
    )
  }

  return null
}

export default ConnectionStatus
