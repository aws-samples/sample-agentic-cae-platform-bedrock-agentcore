import { RouterProvider } from 'react-router-dom'
import { AuthProvider } from './auth/AuthProvider'
import { DesignSelectionProvider } from './panels/results/DesignSelectionContext'
import { DrawingProvider } from './panels/drawing/DrawingContext'
import { SessionProvider } from './session/SessionProvider'
import SessionWebSocketBridge from './session/SessionWebSocketBridge'
import { ThemeProvider } from './theme/ThemeProvider'
import { router } from './router'

/**
 * Provider composition for the app root:
 *   AuthProvider        — authentication state (Req 9.x)
 *   SessionProvider     — session list + active session restoration (Req 13.x)
 *   SessionWebSocketBridge — mounts the WebSocket connection scoped to the
 *                            active session id when one is selected (Req 8.x)
 *   DesignSelectionProvider — shares design selection state between Results
 *                             Dashboard and Viewer Panel (Req 4.3)
 *   RouterProvider      — client-side routing
 *
 * SessionProvider sits inside AuthProvider so it can react to authentication
 * and load the user's session list on sign-in.
 */
function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <SessionProvider>
          <SessionWebSocketBridge>
            <DrawingProvider>
              <DesignSelectionProvider>
                <RouterProvider router={router} />
              </DesignSelectionProvider>
            </DrawingProvider>
          </SessionWebSocketBridge>
        </SessionProvider>
      </AuthProvider>
    </ThemeProvider>
  )
}

export default App
