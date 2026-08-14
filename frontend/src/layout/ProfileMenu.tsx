import { useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { useSession } from '../session/useSession'
import ThemeSwitcher from '../theme/ThemeSwitcher'

/**
 * Profile icon button with a dropdown menu containing the user identity,
 * active session reference, theme switcher, and sign-out action. Keeps the
 * top-right corner clean while surfacing everything an engineer might need
 * without hunting.
 */
function ProfileMenu() {
  const { user, signOut } = useAuth()
  const { activeSessionId } = useSession()
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  return (
    <div className="relative" ref={menuRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="Profile menu"
        aria-expanded={open}
        className="flex h-9 w-9 items-center justify-center rounded-full bg-sky-600 text-sm font-bold text-white shadow-sm transition-colors hover:bg-sky-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500"
      >
        {(user?.username ?? 'U').charAt(0).toUpperCase()}
      </button>

      {open && (
        <>
          {/* Backdrop to close on outside click */}
          <div
            className="fixed inset-0 z-40"
            onClick={() => setOpen(false)}
            aria-hidden="true"
          />
          <div
            className="absolute right-0 top-full z-50 mt-2 w-72 origin-top-right rounded-xl border border-slate-200 bg-white p-3 shadow-lg"
            role="menu"
          >
            {user && (
              <div className="mb-3 border-b border-slate-100 pb-3">
                <p className="truncate text-sm font-semibold text-slate-900">
                  {user.username}
                </p>
                {activeSessionId && (
                  <p className="mt-0.5 truncate text-xs text-slate-400" title={activeSessionId}>
                    Session: {activeSessionId.slice(0, 8)}…
                  </p>
                )}
              </div>
            )}

            <div className="mb-3 flex items-center justify-between">
              <span className="text-xs font-medium text-slate-500">Theme</span>
              <ThemeSwitcher />
            </div>

            <button
              type="button"
              onClick={() => {
                setOpen(false)
                void signOut()
              }}
              role="menuitem"
              className="w-full rounded-lg px-3 py-2 text-left text-sm font-medium text-red-600 transition-colors hover:bg-red-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500"
            >
              Sign out
            </button>
          </div>
        </>
      )}
    </div>
  )
}

export default ProfileMenu
