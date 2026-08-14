import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { getDrawingHistory, getModelGenerationStates } from '../api/sessions'
import { requestModelGeneration } from '../api/generate3DModel'
import { useDrawings } from './drawing/DrawingContext'
import { useSession } from '../session/useSession'
import { WebSocketContext } from '../ws/context'
import type { ClientMessage, ServerMessage, WebSocketLike } from '../ws/types'
import { extractProgress, type OptimizationProgress } from './chat/chatState'
import ConvergenceChart from './results/ConvergenceChart'
import DesignTable from './results/DesignTable'
import DesignComparison from './results/DesignComparison'
import { useDesignSelection } from './results/DesignSelectionContext'
import HistorySelector from './results/HistorySelector'
import type { OptimizationRunSummary } from './results/HistorySelector'
import LoadingAnimation from './results/LoadingAnimation'
import ParetoChart from './results/ParetoChart'
import { buildReportMarkdown, collectConstraintNames, collectObjectiveNames } from './results/report'
import { SAMPLE_RESULT } from './results/sampleResult'
import { MAX_COMPARISON_DESIGNS, toggleSelection } from './results/selection'
import type { ModelGenerationState, OptimizationHistoryEntry, OptimizationResult, ParetoDesign } from './results/types'

interface ResultsDashboardPanelProps {
  /** Explicit result to render; overrides any WebSocket-derived result. */
  result?: OptimizationResult
  /** Notified whenever the compare selection changes (up to 4 ids). */
  onSelectionChange?: (selectedIds: string[]) => void
}

type ResultsTab = 'pareto' | 'convergence' | 'designs'

/** Terminal lifecycle phases: the final outcomes of a generation attempt. */
const TERMINAL_STATUSES: ReadonlySet<ModelGenerationState['status']> = new Set([
  'ready',
  'error',
])

function isTerminalStatus(status: ModelGenerationState['status']): boolean {
  return TERMINAL_STATUSES.has(status)
}

/**
 * Pure, idempotent upsert of a per-design {@link ModelGenerationState} keyed by
 * `designId` (Req 2.4). The WebSocket `model_generated` frame and the REST
 * polling fallback both feed this reducer, so convergence must be independent
 * of arrival order:
 *
 * - A terminal status (`ready`/`error`) supersedes a non-terminal one
 *   (`generating`/`idle`).
 * - When an existing state is already terminal, it is retained and NOT
 *   overwritten, so the first-arriving terminal outcome wins regardless of
 *   which transport delivered it (order-independent convergence).
 * - Otherwise the incoming state replaces the prior (non-terminal) state.
 *
 * Returns a new array; the input array and its elements are never mutated. This
 * function is intentionally free of React state so it can be unit tested in
 * isolation.
 */
export function upsertModelState(
  states: ModelGenerationState[],
  next: ModelGenerationState,
): ModelGenerationState[] {
  const existing = states.find((s) => s.designId === next.designId)
  if (!existing) {
    return [...states, next]
  }
  // First-arriving terminal state is retained: a second terminal (or any
  // non-terminal) update for the same design does not overwrite it.
  if (isTerminalStatus(existing.status)) {
    return states
  }
  // Existing state is non-terminal: the incoming state supersedes it in place
  // (a terminal outcome wins; a fresh non-terminal update is idempotent).
  return states.map((s) => (s.designId === next.designId ? next : s))
}

/**
 * Best-effort narrowing of a WebSocket `result` frame payload into an
 * {@link OptimizationResult}.
 */
function parseResultFrame(message: ServerMessage | null): OptimizationResult | null {
  if (!message || message.type !== 'result') {
    return null
  }
  const payload = message.payload as Partial<OptimizationResult> | null
  if (
    payload &&
    Array.isArray(payload.designs) &&
    Array.isArray(payload.convergence) &&
    typeof payload.populationSize === 'number' &&
    typeof payload.generationCount === 'number'
  ) {
    return payload as OptimizationResult
  }
  return null
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: string
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 ${
        active
          ? 'bg-sky-600 text-white'
          : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
      }`}
    >
      {children}
    </button>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col rounded-md border border-slate-200 bg-slate-50 px-4 py-2">
      <span className="text-lg font-semibold tabular-nums text-slate-900">{value}</span>
      <span className="text-xs uppercase tracking-wide text-slate-500">{label}</span>
    </div>
  )
}

function ResultsDashboardPanel({ result: resultProp, onSelectionChange }: ResultsDashboardPanelProps) {
  const wsContext = useContext(WebSocketContext)
  const { activeSessionId } = useSession()
  const designSelection = useDesignSelection()
  // Optimization history is drawing-scoped: Design Exploration shows every run
  // for the active drawing across all sessions (the default view).
  const drawingCtx = useDrawings()
  const activeDrawingId = drawingCtx?.selectedId ?? null

  // Persist the latest good result + live optimization state, driven by WS
  // frames, so the panel shows the "Optimizing…" animation during a run and
  // keeps the results after a subsequent non-result frame arrives.
  const [capturedResult, setCapturedResult] = useState<OptimizationResult | null>(null)
  const [optimizing, setOptimizing] = useState(false)
  const [progress, setProgress] = useState<OptimizationProgress | null>(null)
  const [tab, setTab] = useState<ResultsTab>('pareto')

  // History state (Req 5.1, 5.2, 5.4, 5.5, 5.6)
  const [optimizationHistory, setOptimizationHistory] = useState<OptimizationHistoryEntry[]>([])
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)

  // Per-design 3D model generation state (Req 3.1, 3.3, 3.4, 3.5)
  const [modelStates, setModelStates] = useState<ModelGenerationState[]>([])

  /**
   * Trigger 3D model generation for a selected design (Req 3.1, 3.5).
   *
   * Transitions the design to `generating` and sends the `generate_model`
   * request via {@link requestModelGeneration}, threading the active
   * `sessionId`/`drawingId` so the backend can resolve the drawing's
   * ground-truth. This is a deliberate force-reset (not the order-independent
   * {@link upsertModelState}) so a retry from a terminal `error` state re-enters
   * `generating`.
   *
   * If the WebSocket context, session, or drawing is missing — or the send
   * throws — only this design transitions to `error` (with a retry affordance
   * surfaced by DesignTable/ViewerPanel); every other design's state is left
   * unchanged (Req 3.5).
   */
  const onGenerate3D = useCallback(
    (design: ParetoDesign) => {
      // Force-replace only this design's state, leaving all others untouched.
      const setDesignState = (state: ModelGenerationState) =>
        setModelStates((prev) => [
          ...prev.filter((s) => s.designId !== design.id),
          state,
        ])

      if (!wsContext || !activeSessionId || !activeDrawingId) {
        setDesignState({
          designId: design.id,
          status: 'error',
          error: 'No active drawing or connection to generate a 3D model.',
        })
        return
      }

      // Optimistically mark the design as generating before the send (Req 3.1).
      setDesignState({ designId: design.id, status: 'generating' })

      // Bridge the request client (which frames a raw wire message) onto the
      // session-scoped WebSocket context, whose send() re-wraps {action, data}
      // with the active session id. This keeps a single source of truth for the
      // generate_model wire frame in requestModelGeneration.
      const socket: WebSocketLike = {
        readyState: 1,
        send: (payload: string) => {
          const frame = JSON.parse(payload) as {
            action: ClientMessage['action']
            data: unknown
          }
          wsContext.send(frame.action, frame.data)
        },
        close: () => {},
        onopen: null,
        onmessage: null,
        onclose: null,
        onerror: null,
      }

      try {
        requestModelGeneration(socket, {
          sessionId: activeSessionId,
          designId: design.id,
          drawingId: activeDrawingId,
          objectives: design.objectives,
        })
      } catch {
        // Delivery failed — transition only this design to error (Req 3.5).
        setDesignState({
          designId: design.id,
          status: 'error',
          error: 'Failed to send the 3D generation request.',
        })
      }
    },
    [wsContext, activeSessionId, activeDrawingId],
  )

  // Tracks the newest run timestamp we've rendered, so the silent auto-refresh
  // only updates state (and resets the selection) when a genuinely new run
  // appears — avoiding disruption when nothing changed.
  const latestTsRef = useRef<string | null>(null)

  // Fetch the active drawing's optimization history (all runs, all sessions).
  const fetchHistory = useCallback(async () => {
    if (!activeDrawingId) {
      setOptimizationHistory([])
      latestTsRef.current = null
      return
    }
    setHistoryLoading(true)
    setHistoryError(null)
    try {
      const runs = await getDrawingHistory(activeDrawingId)
      setOptimizationHistory(runs)
      latestTsRef.current = runs[0]?.timestamp ?? null
      setSelectedIndex(0) // Default to most recent run (Req 5.4)
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : 'Failed to load history')
    } finally {
      setHistoryLoading(false)
    }
  }, [activeDrawingId])

  useEffect(() => {
    void fetchHistory()
  }, [fetchHistory])

  // Restore persisted model-generation states from the backend on mount (or
  // when the active drawing changes) so generated 3D models survive a page
  // refresh. Without this, modelStates starts empty and the viewer loses all
  // ready models until the user re-generates them.
  useEffect(() => {
    if (!activeDrawingId) return
    void getModelGenerationStates(activeDrawingId).then((states) => {
      if (states.length > 0) {
        setModelStates((prev) => {
          let merged = [...prev]
          for (const s of states) {
            merged = upsertModelState(merged, s)
          }
          return merged
        })
      }
    }).catch(() => {
      // Transient failure — keep the current in-memory state, don't break the panel.
    })
  }, [activeDrawingId])

  // Silent auto-refresh: poll the drawing-scoped history while the panel is
  // open so a run that completes (e.g. after a dropped WebSocket) shows up in
  // the History Selector without a manual reload. Only updates state when a
  // new run appears, so it won't clobber a user's manual run selection.
  useEffect(() => {
    if (!activeDrawingId) return
    const id = window.setInterval(() => {
      void getDrawingHistory(activeDrawingId)
        .then((runs) => {
          const newestTs = runs[0]?.timestamp ?? null
          if (newestTs && newestTs !== latestTsRef.current) {
            latestTsRef.current = newestTs
            setOptimizationHistory(runs)
            setSelectedIndex(0)
          }
        })
        .catch(() => {
          /* transient — keep the current view, try again next tick */
        })
    }, 5000)
    return () => window.clearInterval(id)
  }, [activeDrawingId])

  useEffect(() => {
    const message = wsContext?.lastMessage
    if (!message) return
    if (message.type === 'progress') {
      // 3D model generation emits its own `progress` frame (stage
      // "generating_model", carrying a designId). That is NOT an optimization
      // run, so it must not flip Design Exploration into the "Optimizing…"
      // state — otherwise the panel spins forever since the terminal
      // model_generated/model_generation_failed frames don't clear it.
      const progressPayload = message.payload as Record<string, unknown> | null
      if (
        progressPayload &&
        (progressPayload.stage === 'generating_model' || 'designId' in progressPayload)
      ) {
        return
      }
      setOptimizing(true)
      setProgress(extractProgress(message.payload))
    } else if (message.type === 'result') {
      const payload = message.payload as Record<string, unknown> | null

      // Handle terminal 3D model result frames. The backend emits BOTH
      // model_generated (success) and model_generation_failed (failure) as
      // `result`-type frames, keyed by designId (Req 3.3, 3.4).
      if (payload && payload.action === 'model_generated') {
        const designId = payload.designId as string | undefined
        const modelUrl = payload.modelUrl as string | undefined
        if (designId) {
          setModelStates((prev) =>
            upsertModelState(prev, {
              designId,
              status: 'ready' as const,
              modelUrl: modelUrl ?? '',
            }),
          )
        }
        return
      }
      if (payload && payload.action === 'model_generation_failed') {
        const designId = payload.designId as string | undefined
        if (designId) {
          setModelStates((prev) =>
            upsertModelState(prev, {
              designId,
              status: 'error' as const,
              error: (payload.message as string) ?? 'Generation failed',
            }),
          )
        }
        return
      }

      // Handle optimization result frames
      const parsed = parseResultFrame(message)
      if (parsed) {
        setCapturedResult(parsed)
        // When a new result arrives via WS, prepend it to the history (Req 5.3)
        const entry: OptimizationHistoryEntry = {
          ...parsed,
          timestamp: new Date().toISOString(),
        }
        setOptimizationHistory((prev) => [entry, ...prev])
        setSelectedIndex(0)
      }
      setOptimizing(false)
      setProgress(null)
    } else if (message.type === 'error') {
      // Handle model generation errors (Req 3.4)
      const payload = message.payload as Record<string, unknown> | null
      if (payload && payload.action === 'model_generated' && payload.designId) {
        const designId = payload.designId as string
        setModelStates((prev) =>
          upsertModelState(prev, {
            designId,
            status: 'error' as const,
            error: (payload.message as string) ?? 'Generation failed',
          }),
        )
        return
      }
      setOptimizing(false)
      setProgress(null)
    }
  }, [wsContext?.lastMessage])

  // Derive the active result: history entry takes precedence over live/captured data (Req 5.3)
  const historyResult: OptimizationResult | null =
    optimizationHistory.length > 0 && selectedIndex < optimizationHistory.length
      ? optimizationHistory[selectedIndex]
      : null

  const result = resultProp ?? historyResult ?? capturedResult ?? SAMPLE_RESULT
  const hasResults = result.designs.length > 0

  /**
   * Retry 3D generation for a failed design (Req 5.3, 5.6). Re-runs the same
   * {@link onGenerate3D} path ONLY when the design's current status is `error`;
   * for any other status (idle/generating/ready) this is a no-op so an
   * in-flight or completed design is never disturbed.
   */
  const onRetryGeneration = useCallback(
    (designId: string) => {
      const current = modelStates.find((s) => s.designId === designId)
      if (current?.status !== 'error') return
      const design = result.designs.find((d) => d.id === designId)
      if (!design) return
      onGenerate3D(design)
    },
    [modelStates, result.designs, onGenerate3D],
  )

  // Build run summaries for the HistorySelector (Req 5.2)
  const historySummaries: OptimizationRunSummary[] = useMemo(
    () =>
      optimizationHistory.map((entry) => ({
        timestamp: entry.timestamp,
        designCount: entry.designs.length,
        sourceDrawingId: entry.sourceDrawingId,
        generationCount: entry.generationCount,
      })),
    [optimizationHistory],
  )

  const objectiveNames = useMemo(() => collectObjectiveNames(result.designs), [result.designs])
  const constraintNames = useMemo(() => collectConstraintNames(result.designs), [result.designs])

  const [xObjective, setXObjective] = useState('')
  const [yObjective, setYObjective] = useState('')
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  useEffect(() => {
    setXObjective(objectiveNames[0] ?? '')
    setYObjective(objectiveNames[1] ?? objectiveNames[0] ?? '')
  }, [objectiveNames])

  useEffect(() => {
    setSelectedIds([])
  }, [result.designs])

  useEffect(() => {
    onSelectionChange?.(selectedIds)
  }, [selectedIds, onSelectionChange])

  // Sync selectedIds and modelStates to the shared DesignSelectionContext so the
  // ViewerPanel can filter its comparison grid to show only selected designs (Req 4.3).
  useEffect(() => {
    designSelection?.setSelectedIds(selectedIds)
  }, [selectedIds, designSelection])

  useEffect(() => {
    designSelection?.setModelStates(modelStates)
  }, [modelStates, designSelection])

  // Bridge the retry handler to the ViewerPanel (a sibling route) through the
  // shared DesignSelectionContext, mirroring how modelStates/selectedIds are
  // shared. No unmount cleanup: the last handler persists so the Viewer route
  // can retry even while this panel is unmounted (Req 5.3, 5.6).
  useEffect(() => {
    designSelection?.setOnRetryGeneration(onRetryGeneration)
  }, [onRetryGeneration, designSelection])

  const report = useMemo(() => {
    if (result.reportUrl) {
      return { href: result.reportUrl, isBlob: false }
    }
    const markdown = result.reportMarkdown ?? buildReportMarkdown(result)
    const href = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown' }))
    return { href, isBlob: true }
  }, [result])

  useEffect(() => {
    return () => {
      if (report.isBlob) {
        URL.revokeObjectURL(report.href)
      }
    }
  }, [report])

  const handleToggle = (id: string) => {
    setSelectedIds((prev) => toggleSelection(prev, id, MAX_COMPARISON_DESIGNS))
  }

  const selectedDesigns = useMemo(
    () => result.designs.filter((d) => selectedIds.includes(d.id)),
    [result.designs, selectedIds],
  )

  const evaluations = result.populationSize * result.generationCount

  return (
    <section
      className="flex h-full w-full flex-col gap-4 overflow-hidden rounded-lg border border-slate-200 bg-slate-50 p-6 shadow-sm"
      aria-labelledby="results-heading"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 id="results-heading" className="text-2xl font-semibold text-slate-900">
            Design Exploration
          </h1>
          <p className="text-sm text-slate-600">
            Pareto front, convergence, and design comparison for the latest optimization run.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {/* History selector (Req 5.2, 5.4) */}
          <HistorySelector
            runs={historySummaries}
            selectedIndex={selectedIndex}
            onSelect={setSelectedIndex}
            loading={historyLoading}
            resolveDrawingName={(id) => {
              const drawing = drawingCtx?.drawings?.find((d) => d.drawingId === id)
              return drawing?.filename ?? id
            }}
          />
          {hasResults && !optimizing && (
            <a
              href={report.href}
              {...(report.isBlob ? { download: 'optimization-report.md' } : {})}
              className="inline-flex items-center gap-2 rounded-md bg-sky-600 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-sky-700"
            >
              Download report
            </a>
          )}
        </div>
      </header>

      {/* Error state for history fetch failure with retry (Req 5.6) */}
      {historyError && (
        <div className="flex items-center gap-3 rounded-md border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">
          <span>{historyError}</span>
          <button
            type="button"
            onClick={() => void fetchHistory()}
            className="rounded bg-red-100 px-2 py-1 text-xs font-medium text-red-800 hover:bg-red-200"
          >
            Retry
          </button>
        </div>
      )}

      {optimizing ? (
        <LoadingAnimation
          generation={progress?.generation}
          hypervolume={progress?.hypervolume}
          etaSeconds={progress?.etaSeconds}
        />
      ) : !hasResults ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-slate-300 p-10 text-center">
          <p className="text-base font-medium text-slate-700">No optimization results yet</p>
          <p className="max-w-md text-sm text-slate-500">
            Run an optimization from the Chat panel. Once it completes, the Pareto chart,
            convergence plot, and design comparison table will appear here.
          </p>
        </div>
      ) : (
        <>
          {/* Status metrics (parity: evaluations + Pareto size) */}
          <div className="flex flex-wrap gap-3">
            <Stat label="Evaluations" value={evaluations} />
            <Stat label="Population" value={result.populationSize} />
            <Stat label="Generations" value={result.generationCount} />
            <Stat label="Pareto size" value={result.designs.length} />
            {result.sourceDrawingId && <Stat label="Drawing" value={drawingCtx?.drawings?.find((d) => d.drawingId === result.sourceDrawingId)?.filename ?? result.sourceDrawingId} />}
          </div>

          {/* Tabs */}
          <div role="tablist" aria-label="Results views" className="flex gap-1 border-b border-slate-200 pb-2">
            <TabButton active={tab === 'pareto'} onClick={() => setTab('pareto')}>
              Pareto Front
            </TabButton>
            <TabButton active={tab === 'convergence'} onClick={() => setTab('convergence')}>
              Convergence
            </TabButton>
            <TabButton active={tab === 'designs'} onClick={() => setTab('designs')}>
              Designs
            </TabButton>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {tab === 'pareto' && (
              <div className="flex flex-col gap-2 rounded-md border border-slate-200 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 className="text-sm font-semibold text-slate-800">Pareto front</h2>
                  {objectiveNames.length > 2 && (
                    <div className="flex items-center gap-2 text-xs text-slate-600">
                      <label className="flex items-center gap-1">
                        X
                        <select
                          value={xObjective}
                          onChange={(event) => setXObjective(event.target.value)}
                          className="rounded border border-slate-300 bg-slate-50 px-1 py-0.5"
                        >
                          {objectiveNames.map((name) => (
                            <option key={name} value={name}>
                              {name}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="flex items-center gap-1">
                        Y
                        <select
                          value={yObjective}
                          onChange={(event) => setYObjective(event.target.value)}
                          className="rounded border border-slate-300 bg-slate-50 px-1 py-0.5"
                        >
                          {objectiveNames.map((name) => (
                            <option key={name} value={name}>
                              {name}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                  )}
                </div>
                <ParetoChart
                  designs={result.designs}
                  xObjective={xObjective}
                  yObjective={yObjective}
                  selectedIds={selectedIds}
                />
              </div>
            )}

            {tab === 'convergence' && (
              <div className="flex flex-col gap-2 rounded-md border border-slate-200 p-4">
                <h2 className="text-sm font-semibold text-slate-800">Convergence (hypervolume)</h2>
                <ConvergenceChart convergence={result.convergence} />
              </div>
            )}

            {tab === 'designs' && (
              <div className="flex flex-col gap-4">
                <div className="flex flex-col gap-2 rounded-md border border-slate-200 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-sm font-semibold text-slate-800">Pareto designs</h2>
                    <p className="text-xs text-slate-500">
                      Selected {selectedIds.length} / {MAX_COMPARISON_DESIGNS} (also drives
                      side-by-side 3D comparison)
                    </p>
                  </div>
                  <DesignTable
                    designs={result.designs}
                    objectiveNames={objectiveNames}
                    constraintNames={constraintNames}
                    selectedIds={selectedIds}
                    maxSelection={MAX_COMPARISON_DESIGNS}
                    onToggle={handleToggle}
                    modelStates={modelStates}
                    onGenerate3D={onGenerate3D}
                  />
                </div>

                <div className="flex flex-col gap-2 rounded-md border border-slate-200 p-4">
                  <h2 className="text-sm font-semibold text-slate-800">Side-by-side comparison</h2>
                  <DesignComparison
                    designs={selectedDesigns}
                    objectiveNames={objectiveNames}
                    constraintNames={constraintNames}
                  />
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  )
}

export default ResultsDashboardPanel
