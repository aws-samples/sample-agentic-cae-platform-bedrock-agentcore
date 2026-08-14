import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from './AuthContext'

/**
 * Route guard for authenticated-only areas. Unauthenticated users are
 * redirected to `/login` (Req 9.3), preserving the attempted location in
 * router state so the login view can return them there after sign-in.
 *
 * Renders nothing but a lightweight loading state while the initial session
 * hydration is still in flight, to avoid a redirect flash for users who do
 * have a valid session.
 */
function RequireAuth() {
  const { isAuthenticated, isLoading } = useAuth()
  const location = useLocation()

  if (isLoading) {
    return (
      <div className="flex h-full min-h-screen w-full items-center justify-center bg-slate-100 text-slate-500">
        <span className="text-sm">Loading…</span>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }

  return <Outlet />
}

export default RequireAuth
