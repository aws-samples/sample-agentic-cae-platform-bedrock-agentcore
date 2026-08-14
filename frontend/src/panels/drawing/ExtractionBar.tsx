import { useContext, useEffect, useRef, useState } from 'react'
import { WebSocketContext } from '../../ws/context'

/**
 * Simplified horizontal extraction/upload progress bar. Shows the upload +
 * extraction lifecycle as a compact step indicator: Upload → Extract → Index → Done.
 *
 * Since extraction now runs synchronously inside the upload-confirm HTTP call
 * (not streamed via WebSocket), this component is driven by the `phase` prop
 * passed from the parent (WorkspacePanel) rather than listening for WS frames.
 * It falls back to WS `stage` frames for backwards compatibility if the parent
 * doesn't drive it via props.
 */

const STAGES = [
  { key: 'uploading', label: 'Uploading' },
  { key: 'extracting', label: 'Extracting' },
  { key: 'indexing', label: 'Indexing' },
  { key: 'done', label: 'Done' },
] as const

type Phase = 'idle' | 'uploading' | 'confirming' | 'done' | 'error'

function phaseToCompleted(phase: Phase): Set<string> {
  switch (phase) {
    case 'uploading':
      return new Set(['uploading'])
    case 'confirming':
      return new Set(['uploading', 'extracting', 'indexing'])
    case 'done':
      return new Set(['uploading', 'extracting', 'indexing', 'done'])
    default:
      return new Set()
  }
}

function extractStageName(payload: unknown): string | null {
  if (typeof payload === 'string') return payload
  if (payload && typeof payload === 'object') {
    const r = payload as Record<string, unknown>
    const c = r.stage ?? r.name ?? r.type
    if (typeof c === 'string') return c
  }
  return null
}

interface ExtractionBarProps {
  active: boolean
  /** The current upload phase from the parent (preferred over WS frames). */
  phase?: Phase
}

function ExtractionBar({ active, phase = 'idle' }: ExtractionBarProps) {
  const ws = useContext(WebSocketContext)
  const [wsCompleted, setWsCompleted] = useState<Set<string>>(() => new Set())
  const processedSequence = useRef<number>(-1)

  // Listen for WS stage frames as a fallback (backwards-compat).
  useEffect(() => {
    const message = ws?.lastMessage
    if (!message || message.type !== 'stage') return
    if (message.sequence <= processedSequence.current) return
    processedSequence.current = message.sequence
    const stage = extractStageName(message.payload)
    if (stage) {
      setWsCompleted((prev) => new Set(prev).add(stage.toLowerCase()))
    }
  }, [ws?.lastMessage])

  if (!active && phase === 'idle') return null

  // Prefer phase-driven progress; fall back to WS-driven.
  const completed = phase !== 'idle' ? phaseToCompleted(phase) : wsCompleted
  const allDone = completed.has('done') || completed.has('ingestion_complete')

  return (
    <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 shadow-sm">
      <span className="text-[11px] font-semibold text-slate-500">
        {allDone ? '✓ Ready' : phase === 'error' ? '✗ Failed' : '⏳ Processing'}
      </span>
      <div className="flex items-center gap-0.5">
        {STAGES.map((stage, index) => {
          const done = completed.has(stage.key)
          const isCurrent = !done && index > 0 && completed.has(STAGES[index - 1].key)
          return (
            <div key={stage.key} className="flex items-center gap-0.5">
              {index > 0 && (
                <div className={`h-0.5 w-3 rounded-full ${done ? 'bg-emerald-400' : 'bg-slate-200'}`} />
              )}
              <span
                className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                  done
                    ? 'bg-emerald-100 text-emerald-700'
                    : isCurrent
                      ? 'animate-pulse bg-sky-100 text-sky-700'
                      : 'bg-slate-100 text-slate-400'
                }`}
              >
                {done ? '✓' : ''} {stage.label}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default ExtractionBar
