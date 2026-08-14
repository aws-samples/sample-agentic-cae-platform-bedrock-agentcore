import { createBrowserRouter, Navigate } from 'react-router-dom'
import AppLayout from './layout/AppLayout'
import RequireAuth from './auth/RequireAuth'
import LoginPage from './auth/LoginPage'
import WorkspacePanel from './panels/WorkspacePanel'
import ResultsDashboardPanel from './panels/ResultsDashboardPanel'
import ViewerPanel from './panels/ViewerPanel'

/**
 * Client-side route table.
 *
 * `/login` is the public, unauthenticated gate. Everything else lives behind a
 * `RequireAuth` guard that redirects unauthenticated users to `/login`
 * (Req 9.3). All panels remain children of AppLayout, so switching routes swaps
 * only the <Outlet> content without a full page reload (Req 8.1).
 *
 * Sessions, Chat, and the Drawing Viewer no longer have standalone routes —
 * they are composed into the Workspace panel. The legacy `/sessions`,
 * `/chat`, and `/drawings` paths redirect to `/workspace` so existing links and
 * bookmarks keep working. `/` redirects to the Workspace, and unknown paths
 * fall back to it.
 */
export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    element: <RequireAuth />,
    children: [
      {
        path: '/',
        element: <AppLayout />,
        children: [
          { index: true, element: <Navigate to="/workspace" replace /> },
          { path: 'workspace', element: <WorkspacePanel /> },
          // Legacy routes now folded into the Workspace tab.
          { path: 'sessions', element: <Navigate to="/workspace" replace /> },
          { path: 'drawings', element: <Navigate to="/workspace" replace /> },
          { path: 'chat', element: <Navigate to="/workspace" replace /> },
          { path: 'results', element: <ResultsDashboardPanel /> },
          { path: 'viewer', element: <ViewerPanel /> },
          { path: '*', element: <Navigate to="/workspace" replace /> },
        ],
      },
    ],
  },
])
