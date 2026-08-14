import {
  useContext,
  useEffect,
  useReducer,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { WebSocketContext } from '../ws/context'
import type { ServerMessage } from '../ws/types'
import { useSession } from '../session/useSession'
import { getSession } from '../api/sessions'
import {
  appendUserMessage,
  applyServerMessage,
  hydrateChatState,
  initialChatState,
  type ChatMessage,
  type ChatState,
  type OptimizationProgress,
} from './chat/chatState'
import Markdown from './chat/Markdown'
import ElapsedTimer from './chat/ElapsedTimer'
import {
  extractSuggestions,
  stripSuggestionLines,
  STARTER_SUGGESTIONS,
} from './chat/suggestions'

type ChatAction =
  | { kind: 'server'; message: ServerMessage }
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string }
  | { kind: 'hydrate'; messages: readonly unknown[] }

function chatReducer(state: ChatState, action: ChatAction): ChatState {
  if (action.kind === 'server') {
    return applyServerMessage(state, action.message)
  }
  if (action.kind === 'hydrate') {
    return hydrateChatState(action.messages)
  }
  if (action.kind === 'assistant') {
    // Append a finalized assistant message WITHOUT advancing `lastSequence`
    // (used by the REST polling fallback), so any later real WebSocket frames
    // for this session are still applied in order.
    const assistantMessage: ChatMessage = {
      id: action.id,
      role: 'assistant',
      text: action.text,
      streaming: false,
    }
    return {
      ...state,
      messages: [...state.messages, assistantMessage],
      optimizing: false,
      progress: null,
    }
  }
  return appendUserMessage(state, action.id, action.text)
}

/** A pending request kept so the user can retry after an error. */
interface PendingRequest {
  action: 'sendmessage' | 'optimize'
  text: string
}

/** Format an ETA in seconds as a compact human string. */
function formatEta(etaSeconds: number | undefined): string {
  if (etaSeconds === undefined) {
    return '—'
  }
  if (etaSeconds < 60) {
    return `${Math.round(etaSeconds)}s`
  }
  const minutes = Math.floor(etaSeconds / 60)
  const seconds = Math.round(etaSeconds % 60)
  return `${minutes}m ${seconds}s`
}

function MessageBubble({
  message,
  onSuggestion,
}: {
  message: ChatMessage
  onSuggestion: (query: string) => void
}) {
  const isUser = message.role === 'user'
  // Assistant answers render as markdown; extract any quoted follow-up
  // suggestions and strip them from the body so they aren't shown twice.
  const suggestions = !isUser && !message.streaming ? extractSuggestions(message.text) : []
  const body = isUser ? message.text : stripSuggestionLines(message.text)
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed shadow-sm ${
          isUser
            ? 'bg-sky-600 text-white'
            : 'border border-slate-200 bg-slate-50 text-slate-800'
        }`}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap break-words">{message.text}</p>
        ) : (
          <div className="break-words">
            <Markdown>{body}</Markdown>
            {message.streaming ? (
              <span
                aria-hidden="true"
                className="ml-0.5 inline-block h-4 w-1.5 animate-pulse rounded-sm bg-current align-middle"
              />
            ) : null}
          </div>
        )}
        {suggestions.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-2 border-t border-slate-200 pt-2">
            {suggestions.map((query) => (
              <button
                key={query}
                type="button"
                onClick={() => onSuggestion(query)}
                className="rounded-full border border-sky-300 bg-sky-50 px-3 py-1 text-xs font-medium text-sky-700 transition-colors hover:bg-sky-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500"
              >
                {query}
              </button>
            ))}
          </div>
        ) : null}
        {message.sources && message.sources.length > 0 ? (
          <div className="mt-2 border-t border-slate-200 pt-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Sources
            </p>
            <ul className="mt-1 flex flex-col gap-1">
              {message.sources.map((source, index) => (
                <li
                  key={`${source.drawingId}-${index}`}
                  className="text-xs text-slate-600"
                >
                  <span className="font-medium text-slate-800">
                    {source.drawingId}
                  </span>
                  {source.section ? ` · ${source.section}` : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  )
}

function ProgressIndicator({
  progress,
}: {
  progress: OptimizationProgress
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex flex-wrap items-center gap-x-6 gap-y-1 rounded-lg border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900"
    >
      <span className="inline-flex items-center gap-2 font-medium">
        <span
          aria-hidden="true"
          className="h-2 w-2 animate-pulse rounded-full bg-indigo-500"
        />
        Optimizing…
      </span>
      <span>
        Generation:{' '}
        <span className="font-semibold">{progress.generation ?? '—'}</span>
      </span>
      <span>
        Hypervolume:{' '}
        <span className="font-semibold">
          {progress.hypervolume !== undefined
            ? progress.hypervolume.toFixed(4)
            : '—'}
        </span>
      </span>
      <span>
        ETA: <span className="font-semibold">{formatEta(progress.etaSeconds)}</span>
      </span>
      <span>
        Elapsed:{' '}
        <ElapsedTimer className="font-semibold" />
      </span>
    </div>
  )
}

function ChatPanel({ uploadSlot, activeDrawingId }: { uploadSlot?: React.ReactNode; activeDrawingId?: string } = {}) {
  const ws = useContext(WebSocketContext)
  const { activeSession, activeSessionId } = useSession()
  const [state, dispatch] = useReducer(chatReducer, initialChatState)
  const [input, setInput] = useState('')
  const [pending, setPending] = useState<PendingRequest | null>(null)
  const [thinking, setThinking] = useState(false)
  const userSeqRef = useRef(0)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const hydratedFor = useRef<string | null>(null)
  // REST polling fallback for optimize: if the WebSocket `result` frame never
  // arrives (e.g. the connection drops during the ~30s NSGA-II run), poll the
  // session-detail REST endpoint for the newly-persisted optimization run so
  // the chat resolves instead of hanging on "Crew is on it…".
  const optimizePollRef = useRef<number | null>(null)
  const optimizeStartRef = useRef<string | null>(null)

  const isConnected = ws !== null && ws.status === 'open'

  // Rehydrate the conversation from persisted history whenever the active
  // session's detail loads (Req 13.3). Hydrate once per session id so live
  // streamed messages are not clobbered by a later re-render.
  useEffect(() => {
    if (
      activeSessionId !== null &&
      activeSession?.sessionId === activeSessionId &&
      hydratedFor.current !== activeSessionId
    ) {
      hydratedFor.current = activeSessionId
      dispatch({ kind: 'hydrate', messages: activeSession.messages ?? [] })
    }
  }, [activeSessionId, activeSession])

  // Feed every new server message through the pure reducer. The provider
  // replaces `lastMessage` with a fresh object per frame, so this fires once
  // per message. Also clears the "thinking" state on any response.
  useEffect(() => {
    if (ws?.lastMessage) {
      dispatch({ kind: 'server', message: ws.lastMessage })
      // A terminal WebSocket frame beat the poller — stop the fallback.
      if (ws.lastMessage.type === 'result' || ws.lastMessage.type === 'error') {
        stopOptimizePolling()
        setThinking(false)
      } else if (ws.lastMessage.type !== 'progress' && ws.lastMessage.type !== 'stage') {
        setThinking(false)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws?.lastMessage])

  // Clear any in-flight poller when the component unmounts.
  useEffect(() => {
    return () => stopOptimizePolling()
  }, [])

  function stopOptimizePolling(): void {
    if (optimizePollRef.current !== null) {
      window.clearInterval(optimizePollRef.current)
      optimizePollRef.current = null
    }
    optimizeStartRef.current = null
  }

  /**
   * Poll the session-detail REST endpoint for a newly-completed optimization
   * run (persisted to the history table by the backend) and finalize the chat
   * when one appears. This makes optimization delivery resilient to WebSocket
   * disconnects: the history table is the source of truth.
   */
  function startOptimizePolling(sessionId: string): void {
    stopOptimizePolling()
    const startedAt = new Date().toISOString()
    optimizeStartRef.current = startedAt
    const POLL_INTERVAL_MS = 4000
    const MAX_ATTEMPTS = 30 // ~2 minutes
    let attempts = 0

    optimizePollRef.current = window.setInterval(() => {
      attempts += 1
      void getSession(sessionId)
        .then((detail) => {
          const runs = detail.optimizationHistory ?? []
          // A run newer than when we submitted means the optimization finished.
          const fresh = runs.find(
            (r) => typeof r.timestamp === 'string' && r.timestamp >= startedAt,
          )
          if (fresh) {
            const designCount = Array.isArray(fresh.designs) ? fresh.designs.length : 0
            const summary =
              `**Optimization complete.** Found ${designCount} Pareto-optimal ` +
              `design${designCount === 1 ? '' : 's'} ` +
              `(population ${fresh.populationSize}, ${fresh.generationCount} generations). ` +
              `Open the **Design Exploration** tab to explore the Pareto front and convergence.`
            dispatch({ kind: 'assistant', id: `poll-${Date.now()}`, text: summary })
            setThinking(false)
            setPending(null)
            stopOptimizePolling()
            return
          }
          if (attempts >= MAX_ATTEMPTS) {
            dispatch({
              kind: 'assistant',
              id: `poll-${Date.now()}`,
              text:
                'The optimization is taking longer than expected. It may still ' +
                'complete — check the **Design Exploration** tab shortly.',
            })
            setThinking(false)
            stopOptimizePolling()
          }
        })
        .catch(() => {
          // Transient fetch error — keep polling until MAX_ATTEMPTS.
          if (attempts >= MAX_ATTEMPTS) {
            setThinking(false)
            stopOptimizePolling()
          }
        })
    }, POLL_INTERVAL_MS)
  }

  // Keep the newest message in view as the conversation grows.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [state.messages, state.progress])

  function submitRequest(action: PendingRequest['action'], text: string) {
    const trimmed = text.trim()
    if (trimmed.length === 0 || ws === null) {
      return
    }
    const id = `u-${userSeqRef.current}`
    userSeqRef.current += 1
    dispatch({ kind: 'user', id, text: trimmed })
    setPending({ action, text: trimmed })
    setThinking(true)
    const payload: Record<string, string> =
      action === 'optimize' ? { instruction: trimmed } : { text: trimmed }
    // Include the active drawing so the QA agent can scope its tools
    if (activeDrawingId) {
      payload.active_drawing = activeDrawingId
    }
    ws.send(action, payload)
    // For optimization (long-running, ~30s), start a REST polling fallback so
    // a dropped WebSocket doesn't leave the chat hanging (Async REST + polling).
    if (action === 'optimize' && activeSessionId) {
      startOptimizePolling(activeSessionId)
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!isConnected) {
      return
    }
    submitRequest('sendmessage', input)
    setInput('')
  }

  function handleOptimize() {
    if (!isConnected) {
      return
    }
    submitRequest('optimize', input)
    setInput('')
  }

  function handleRetry() {
    if (pending === null || ws === null) {
      return
    }
    const payload: Record<string, string> =
      pending.action === 'optimize'
        ? { instruction: pending.text }
        : { text: pending.text }
    if (activeDrawingId) {
      payload.active_drawing = activeDrawingId
    }
    ws.send(pending.action, payload)
  }

  function handleSuggestion(query: string) {
    if (!isConnected) {
      return
    }
    submitRequest('sendmessage', query)
  }

  return (
    <section
      className="flex h-full w-full flex-col overflow-hidden rounded-lg border border-slate-200 bg-slate-50 shadow-sm"
      aria-labelledby="chat-heading"
    >
      <header className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-4 py-2">
        <h1 id="chat-heading" className="text-sm font-semibold text-slate-700">
          Ask about your drawings
        </h1>
        {!isConnected ? (
          <span
            role="status"
            className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-2.5 py-1 text-[11px] font-medium text-amber-800"
          >
            <span
              aria-hidden="true"
              className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500"
            />
            Connecting…
          </span>
        ) : null}
      </header>

      <div
        ref={scrollRef}
        className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-3"
      >
        {state.messages.length === 0 ? (
          <div className="m-auto flex max-w-lg flex-col items-center gap-4 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-sky-100 text-2xl">
              🔧
            </div>
            <div className="flex flex-col gap-1">
              <p className="text-base font-semibold text-slate-800">
                Welcome to the Agentic CAE Platform
              </p>
              <p className="text-sm text-slate-500">
                Ask questions about your uploaded drawings — materials, dimensions,
                GD&amp;T, and tolerances — or request a multi-objective design
                optimization. Answers cite the source drawings they came from.
              </p>
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              {STARTER_SUGGESTIONS.map((query) => (
                <button
                  key={query}
                  type="button"
                  onClick={() => handleSuggestion(query)}
                  disabled={!isConnected}
                  className="rounded-full border border-slate-300 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {query}
                </button>
              ))}
            </div>
          </div>
        ) : (
          state.messages.map((message) => (
            <MessageBubble
              key={message.id}
              message={message}
              onSuggestion={handleSuggestion}
            />
          ))
        )}

        {state.progress !== null ? (
          <ProgressIndicator progress={state.progress} />
        ) : null}

        {thinking && state.progress === null ? (
          <div className="flex justify-start">
            <div className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm text-slate-600 shadow-sm">
              <span className="flex gap-1">
                <span className="h-2 w-2 animate-bounce rounded-full bg-sky-500 [animation-delay:0ms]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-sky-500 [animation-delay:150ms]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-sky-500 [animation-delay:300ms]" />
              </span>
              <span className="text-xs font-medium text-slate-500">Crew is on it…</span>
              <ElapsedTimer className="text-xs font-medium text-slate-400" />
            </div>
          </div>
        ) : null}

        {state.error !== null ? (
          <div
            role="alert"
            className="flex flex-col gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
          >
            <p className="font-medium">{state.error}</p>
            <p className="text-red-700">
              Try rephrasing your question with more detail, or retry the last
              request.
            </p>
            {pending !== null ? (
              <div>
                <button
                  type="button"
                  onClick={handleRetry}
                  disabled={!isConnected}
                  className="rounded bg-red-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Retry
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      <form
        onSubmit={handleSubmit}
        className="flex items-end gap-2 border-t border-slate-200 bg-slate-50 px-4 py-3"
      >
        {uploadSlot}
        <label htmlFor="chat-input" className="sr-only">
          Message
        </label>
        <textarea
          id="chat-input"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault()
              handleSubmit(
                event as unknown as FormEvent<HTMLFormElement>,
              )
            }
          }}
          rows={1}
          placeholder={
            isConnected ? 'Ask a question…' : 'Connecting to the platform…'
          }
          disabled={!isConnected}
          className="max-h-40 min-h-[2.75rem] flex-1 resize-y rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 shadow-sm focus-visible:border-sky-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-sky-500 disabled:cursor-not-allowed disabled:bg-slate-100"
        />
        <button
          type="button"
          onClick={handleOptimize}
          disabled={!isConnected || input.trim().length === 0}
          className="rounded-lg border border-indigo-300 bg-indigo-50 px-4 py-2.5 text-sm font-semibold text-indigo-700 transition-colors hover:bg-indigo-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Optimize
        </button>
        <button
          type="submit"
          disabled={!isConnected || input.trim().length === 0}
          className="rounded-lg bg-sky-600 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-sky-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </section>
  )
}

export default ChatPanel
