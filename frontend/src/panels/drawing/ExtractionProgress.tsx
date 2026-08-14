import { useContext, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { WebSocketContext } from '../../ws/context'

/** Ordered extraction stages surfaced to the user (Req 8.2, 12.3). */
const STAGES = [
  { key: 'parsing_started', label: 'Parsing started' },
  { key: 'bom_extracted', label: 'BOM extracted' },
  { key: 'gdt_extracted', label: 'GD&T extracted' },
  { key: 'ingestion_complete', label: 'Ingestion complete' },
] as const

/** Stage names (or aliases) that mean the drawing is indexed and Q&A is ready. */
const COMPLETION_STAGES = new Set(['ingestion_complete', 'indexed'])

/** Delay before auto-navigating to Chat, kept comfortably under the 2s bound. */
const NAVIGATE_DELAY_MS = 1000

/**
 * Best-effort extraction of a stage name from a `stage` frame payload. The
 * backend payload shape is not strongly typed here, so we accept either a bare
 * string or an object carrying `stage` / `name` / `type`.
 */
function extractStageName(payload: unknown): string | null {
  if (typeof payload === 'string') {
    return payload
  }
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    const candidate = record.stage ?? record.name ?? record.type
    if (typeof candidate === 'string') {
      return candidate
    }
  }
  return null
}

interface ExtractionProgressProps {
  /** True once an upload has started, so the checklist is shown. */
  active: boolean
}

/**
 * Renders the extraction stage checklist driven by WebSocket `stage` frames and
 * auto-navigates to the Chat (Q&A) view within 2 seconds of the drawing being
 * indexed (Req 8.2).
 *
 * The WebSocket context is read defensively via {@link WebSocketContext}: the
 * provider is not yet mounted at the app root, so `useWebSocket()` would throw.
 * When the context is absent we render an informational idle state instead.
 */
function ExtractionProgress({ active }: ExtractionProgressProps) {
  const ws = useContext(WebSocketContext)
  const navigate = useNavigate()
  const [completed, setCompleted] = useState<Set<string>>(() => new Set())
  const processedSequence = useRef<number>(-1)
  const hasNavigated = useRef(false)

  // Accumulate completed stages from incoming `stage` frames. `lastMessage`
  // only carries the latest frame, so we guard on `sequence` to process each
  // frame exactly once.
  useEffect(() => {
    const message = ws?.lastMessage
    if (!message || message.type !== 'stage') {
      return
    }
    if (message.sequence <= processedSequence.current) {
      return
    }
    processedSequence.current = message.sequence
    const stage = extractStageName(message.payload)
    if (stage) {
      setCompleted((prev) => {
        const next = new Set(prev)
        next.add(stage.toLowerCase())
        return next
      })
    }
  }, [ws?.lastMessage])

  // Once the drawing is indexed, transition to the Workspace (which houses the
  // chat) within 2 seconds (Req 8.2).
  useEffect(() => {
    if (hasNavigated.current) {
      return
    }
    const isComplete = [...completed].some((stage) => COMPLETION_STAGES.has(stage))
    if (!isComplete) {
      return
    }
    hasNavigated.current = true
    const timer = setTimeout(() => navigate('/workspace'), NAVIGATE_DELAY_MS)
    return () => clearTimeout(timer)
  }, [completed, navigate])

  if (!active) {
    return null
  }

  const isConnected = ws !== null

  return (
    <section
      aria-label="Extraction progress"
      className="rounded-lg border border-slate-200 bg-slate-50 p-4 shadow-sm"
    >
      <h2 className="mb-3 text-sm font-semibold text-slate-900">Extraction progress</h2>
      <ol className="flex flex-col gap-2">
        {STAGES.map((stage) => {
          const done = completed.has(stage.key)
          return (
            <li key={stage.key} className="flex items-center gap-3 text-sm">
              <span
                aria-hidden="true"
                className={`flex h-5 w-5 items-center justify-center rounded-full text-xs font-bold ${
                  done ? 'bg-emerald-500 text-white' : 'bg-slate-200 text-slate-400'
                }`}
              >
                {done ? '✓' : ''}
              </span>
              <span className={done ? 'font-medium text-slate-900' : 'text-slate-500'}>
                {stage.label}
              </span>
            </li>
          )
        })}
      </ol>
      {!isConnected && (
        <p className="mt-3 text-xs text-slate-500">
          Live progress appears here once the real-time connection is active.
        </p>
      )}
    </section>
  )
}

export default ExtractionProgress
