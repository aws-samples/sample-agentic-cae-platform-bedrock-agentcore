import { useNavigate } from 'react-router-dom'
import { useSession } from './useSession'
import { formatLastAccess, formatMessageCount } from './helpers'

/**
 * Session list panel (Req 13.2, 13.4, 13.6).
 *
 * Renders the authenticated user's previous sessions ordered most-recent-first,
 * each showing a title/summary, last-access date, and message count (Req 13.6).
 * Selecting a session restores its detail via the session context (Req 13.3)
 * and navigates into the app; the list itself surfaces loading and error states
 * with a Retry action that does not change the route (Req 13.4).
 */
function SessionList() {
  const {
    sessions,
    listState,
    listError,
    activeSessionId,
    loadState,
    error,
    selectSession,
    startNewSession,
    retry,
  } = useSession()
  const navigate = useNavigate()

  async function handleSelect(sessionId: string): Promise<void> {
    await selectSession(sessionId)
    // Route the user into the workspace once restoration is underway; the
    // panels read the restored data from the session context.
    navigate('/workspace')
  }

  function handleNewChat(): void {
    startNewSession()
    navigate('/workspace')
  }

  return (
    <section
      className="flex h-full w-full flex-col overflow-hidden rounded-lg border border-slate-200 bg-slate-50 shadow-sm"
      aria-labelledby="sessions-heading"
    >
      <header className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-6 py-4">
        <div>
          <h1 id="sessions-heading" className="text-xl font-semibold text-slate-900">
            Sessions
          </h1>
          <p className="text-sm text-slate-500">
            Pick up a previous session to restore its drawings, chat, and results.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleNewChat}
            className="rounded-md bg-sky-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500"
          >
            + New chat
          </button>
          <button
            type="button"
            onClick={() => void retry()}
            disabled={listState === 'loading'}
            className="rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
      </header>

      <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-6 py-4">
        {listState === 'loading' ? (
          <p role="status" className="m-auto text-sm text-slate-400">
            Loading sessions…
          </p>
        ) : null}

        {listState === 'error' ? (
          <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-slate-300 bg-slate-50 px-4 py-6 text-center">
            <p className="text-sm font-medium text-slate-700">
              Saved session history isn&apos;t available yet
            </p>
            <p className="max-w-md text-xs text-slate-500">
              The session history service is not deployed, so previous
              conversations can&apos;t be listed. You can still start a new chat —
              your current conversation and uploads work normally.
            </p>
            <details className="text-xs text-slate-400">
              <summary className="cursor-pointer">Details</summary>
              <span>{listError ?? 'Failed to load sessions.'}</span>
            </details>
            <div className="mt-1 flex gap-2">
              <button
                type="button"
                onClick={handleNewChat}
                className="rounded bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-sky-700"
              >
                + New chat
              </button>
              <button
                type="button"
                onClick={() => void retry()}
                className="rounded border border-slate-300 px-3 py-1.5 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-100"
              >
                Retry
              </button>
            </div>
          </div>
        ) : null}

        {loadState === 'error' ? (
          <div
            role="alert"
            className="flex flex-col gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
          >
            <p className="font-medium">{error ?? 'Failed to load the selected session.'}</p>
            <div>
              <button
                type="button"
                onClick={() => void retry()}
                className="rounded bg-red-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500"
              >
                Retry
              </button>
            </div>
          </div>
        ) : null}

        {listState === 'idle' && sessions.length === 0 ? (
          <p className="m-auto max-w-md text-center text-sm text-slate-400">
            No previous sessions yet. Upload a drawing or start a chat to create one.
          </p>
        ) : null}

        {sessions.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {sessions.map((session) => {
              const isActive = session.sessionId === activeSessionId
              const isRestoring = isActive && loadState === 'loading'
              return (
                <li key={session.sessionId}>
                  <button
                    type="button"
                    onClick={() => void handleSelect(session.sessionId)}
                    aria-current={isActive ? 'true' : undefined}
                    className={`flex w-full flex-col gap-1 rounded-lg border px-4 py-3 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 ${
                      isActive
                        ? 'border-sky-400 bg-sky-50'
                        : 'border-slate-200 bg-slate-50 hover:border-slate-300 hover:bg-slate-50'
                    }`}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-semibold text-slate-900">
                        {session.title}
                      </span>
                      {isRestoring ? (
                        <span className="shrink-0 text-xs font-medium text-sky-600">
                          Restoring…
                        </span>
                      ) : null}
                    </span>
                    <span className="flex flex-wrap items-center gap-x-4 gap-y-0.5 text-xs text-slate-500">
                      <span>{formatLastAccess(session.lastAccessAt)}</span>
                      <span>{formatMessageCount(session.messageCount)}</span>
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        ) : null}
      </div>
    </section>
  )
}

export default SessionList
