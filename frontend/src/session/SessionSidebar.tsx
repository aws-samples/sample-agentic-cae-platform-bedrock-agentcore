import { useState } from 'react'
import { useSession } from './useSession'
import { formatLastAccess } from './helpers'

interface SessionSidebarProps {
  onCollapse?: () => void
}

/**
 * CAE Platform conversation sidebar. Lists the user's sessions,
 * with a delete control (trash icon on hover) and creative auto-generated titles
 * (e.g. "Service Bay #3 — Jul 6"). The active session is highlighted; clicking
 * switches; the "+ New Session" button creates a fresh one.
 */
function SessionSidebar({ onCollapse }: SessionSidebarProps = {}) {
  const {
    sessions,
    listState,
    activeSessionId,
    loadState,
    createSession,
    selectSession,
    deleteSession,
  } = useSession()
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  return (
    <aside className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between gap-2 border-b border-slate-100 px-3 py-2">
        <div className="flex items-center gap-1.5">
          {onCollapse ? (
            <button
              type="button"
              onClick={onCollapse}
              aria-label="Collapse"
              title="Collapse"
              className="rounded-md p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700"
            >
              <svg aria-hidden="true" viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 5l-5 5 5 5" />
              </svg>
            </button>
          ) : null}
          <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500">Sessions</h2>
        </div>
        <button
          type="button"
          onClick={() => void createSession()}
          disabled={loadState === 'loading'}
          className="rounded-md bg-sky-600 px-2 py-1 text-[11px] font-bold text-white transition-colors hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          + New
        </button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto p-1.5">
        {listState === 'loading' && sessions.length === 0 ? (
          <p className="px-2 py-4 text-center text-xs text-slate-400">Loading…</p>
        ) : null}

        {listState === 'idle' && sessions.length === 0 ? (
          <p className="px-2 py-4 text-center text-xs text-slate-400">
            No sessions yet. Click "+ New" to start.
          </p>
        ) : null}

        {sessions.map((session) => {
          const isActive = session.sessionId === activeSessionId
          const isConfirming = confirmDelete === session.sessionId
          return (
            <div
              key={session.sessionId}
              className={`group relative flex w-full items-start gap-1 rounded-md px-2 py-1.5 text-left transition-colors ${
                isActive
                  ? 'bg-sky-50 ring-1 ring-sky-200'
                  : 'hover:bg-slate-50'
              }`}
            >
              <button
                type="button"
                onClick={() => void selectSession(session.sessionId)}
                aria-current={isActive ? 'true' : undefined}
                className="flex min-w-0 flex-1 flex-col gap-0.5"
              >
                <span className={`truncate text-[13px] font-medium ${isActive ? 'text-sky-800' : 'text-slate-700'}`}>
                  {session.title}
                </span>
                <span className="truncate text-[10px] text-slate-400">
                  {formatLastAccess(session.lastAccessAt)}
                  {session.messageCount > 0 ? ` · ${session.messageCount} msgs` : ''}
                </span>
              </button>

              {/* Delete button — shows on hover or when confirming */}
              {isConfirming ? (
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => {
                      setConfirmDelete(null)
                      void deleteSession(session.sessionId)
                    }}
                    className="rounded px-1.5 py-0.5 text-[10px] font-bold text-red-600 hover:bg-red-100"
                    title="Confirm delete"
                  >
                    Yes
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(null)}
                    className="rounded px-1.5 py-0.5 text-[10px] font-bold text-slate-500 hover:bg-slate-100"
                  >
                    No
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirmDelete(session.sessionId)}
                  className="mt-0.5 hidden rounded p-0.5 text-slate-300 transition-colors hover:bg-red-50 hover:text-red-500 group-hover:block"
                  title="Delete this session"
                  aria-label={`Delete ${session.title}`}
                >
                  <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clipRule="evenodd" />
                  </svg>
                </button>
              )}
            </div>
          )
        })}
      </div>
    </aside>
  )
}

export default SessionSidebar
