import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { WebSocketContext } from '../ws/context'
import { useSession } from '../session/useSession'
import { getModelGenerationStates } from '../api/sessions'
import { useDrawings } from './drawing/DrawingContext'
import ModelCanvas from './viewer/ModelCanvas'
import DrawingPane from './viewer/DrawingPane'
import SpecSidebar from './viewer/SpecSidebar'
import { capModelSelection } from './viewer/geometry'
import { deriveModelsFromPayload } from './viewer/deriveModels'
import { MAX_MODELS } from './viewer/types'
import type {
  Annotation,
  DrawingSource,
  ModelSource,
  ModelViewHandle,
} from './viewer/types'
import type { ModelGenerationState } from './results/types'
import { useDesignSelection } from './results/DesignSelectionContext'

/** Stable empty-annotations reference so ModelCanvas doesn't rebuild each render. */
const NO_ANNOTATIONS: readonly Annotation[] = []

/**
 * A generated model associated with its source session and design for
 * traceability (Req 4.2).
 */
interface GeneratedModel {
  sessionId: string
  designId: string
  modelUrl: string
}

interface ViewerPanelProps {
  /**
   * Models to display. Primary path is typed props/state; when omitted the panel
   * derives models from the latest WebSocket `result` payload, or shows the
   * empty state. Capped at {@link MAX_MODELS} (Req 6.5).
   */
  models?: readonly ModelSource[]
  /** Optional 2D drawing shown beside a single 3D model (Req 6.3). */
  drawing?: DrawingSource | null
  /**
   * Model generation states from the Results Dashboard. When a design's status
   * transitions to 'ready' with a modelUrl, the viewer automatically loads and
   * displays the STL (Req 4.1, 4.2).
   */
  modelStates?: readonly ModelGenerationState[]
  /**
   * Callback to retry 3D model generation for a design that failed (Req 4.5).
   * Invoked with the design ID whose generation should be retried.
   */
  onRetryGeneration?: (designId: string) => void
}

/** Grid column classes for the comparison layout (1–4 models). */
function gridClass(count: number): string {
  if (count <= 1) return 'grid-cols-1'
  return 'grid-cols-1 md:grid-cols-2'
}

/** Shared styling for the toolbar buttons. */
const TOOLBAR_BTN =
  'rounded-md border border-slate-300 bg-slate-50 px-2.5 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 disabled:cursor-not-allowed disabled:opacity-40'

/**
 * 3D Viewer panel (Requirements 6.2–6.5). Renders generated STL models in a
 * Three.js scene with orbit/zoom/pan, camera presets (Front/Top/Right), reset,
 * an axes toggle, wireframe, cross-section, and PNG screenshot — plus a part
 * spec sidebar (material, dimensions, GD&T, notes). Overlays clickable
 * annotations, shows the 2D drawing beside a single model, and compares up to
 * four models side-by-side.
 */
function ViewerPanel({ models, drawing = null, modelStates, onRetryGeneration }: ViewerPanelProps) {
  // Defensive WS read: the provider is not mounted yet, so guard for null and
  // never call the `useWebSocket` hook (which would throw without a provider).
  const ws = useContext(WebSocketContext)
  const { activeSessionId } = useSession()
  const drawingCtx = useDrawings()
  const activeDrawingId = drawingCtx?.selectedId ?? null

  // Read shared selection state from the DesignSelectionContext (Req 4.3).
  // When the Results Dashboard selects multiple Pareto designs, their IDs and
  // model states propagate here so we can filter the comparison grid.
  const designSelection = useDesignSelection()
  const contextSelectedIds = designSelection?.selectedIds ?? []
  const contextModelStates = designSelection?.modelStates
  // Prefer an explicit prop; otherwise fall back to the retry handler the
  // Results Dashboard registers on the shared context (Req 4.5, 5.3).
  const retryGeneration = onRetryGeneration ?? designSelection?.onRetryGeneration ?? undefined

  const [wireframe, setWireframe] = useState(false)
  const [clipEnabled, setClipEnabled] = useState(false)
  const [clipOffset, setClipOffset] = useState(0.5)
  const [axes, setAxes] = useState(false)

  // Track generated models with their source session/design for traceability (Req 4.2).
  // Populated from modelStates prop or WebSocket `model_generated` result frames.
  const [generatedModels, setGeneratedModels] = useState<GeneratedModel[]>([])

  // Restore persisted model-generation states from the backend on mount so
  // previously generated 3D models survive page refreshes / direct navigation
  // to the Post-Processing tab without first visiting the Results Dashboard.
  useEffect(() => {
    if (!activeDrawingId || !activeSessionId) return
    void getModelGenerationStates(activeDrawingId).then((states) => {
      const ready = states.filter((s) => s.status === 'ready' && s.modelUrl)
      if (ready.length === 0) return
      setGeneratedModels((prev) => {
        let updated = [...prev]
        for (const state of ready) {
          const exists = updated.some((m) => m.designId === state.designId)
          if (!exists && state.modelUrl) {
            updated = [...updated, { sessionId: activeSessionId, designId: state.designId, modelUrl: state.modelUrl }]
          }
        }
        return updated
      })
    }).catch(() => { /* transient — keep current state */ })
  }, [activeDrawingId, activeSessionId])  // Listen for `model_generated` WebSocket result frames and automatically load
  // the model (Req 4.1). This allows the ViewerPanel to pick up new models even
  // when modelStates is not explicitly passed as a prop.
  useEffect(() => {
    const message = ws?.lastMessage
    if (!message || message.type !== 'result') return

    const payload = message.payload as Record<string, unknown> | null
    if (!payload || payload.action !== 'model_generated') return

    const modelUrl = payload.modelUrl as string | undefined
    const designId = payload.designId as string | undefined
    const sessionId = (payload.session_id as string) ?? (payload.sessionId as string) ?? activeSessionId

    if (modelUrl && designId && sessionId) {
      setGeneratedModels((prev) => {
        // Replace existing entry for the same design, or append
        const filtered = prev.filter((m) => m.designId !== designId)
        return [...filtered, { sessionId, designId, modelUrl }]
      })
    }
  }, [ws?.lastMessage, activeSessionId])

  // Sync modelStates prop into generatedModels when designs become ready (Req 4.1).
  // This handles the case where the ResultsDashboardPanel passes modelStates down.
  useEffect(() => {
    const states = modelStates ?? contextModelStates
    if (!states || !activeSessionId) return

    const readyModels = states.filter(
      (s) => s.status === 'ready' && s.modelUrl,
    )
    if (readyModels.length === 0) return

    setGeneratedModels((prev) => {
      let updated = [...prev]
      for (const state of readyModels) {
        const exists = updated.some((m) => m.designId === state.designId)
        if (!exists && state.modelUrl) {
          updated = [
            ...updated.filter((m) => m.designId !== state.designId),
            {
              sessionId: activeSessionId,
              designId: state.designId,
              modelUrl: state.modelUrl,
            },
          ]
        }
      }
      return updated
    })
  }, [modelStates, contextModelStates, activeSessionId])

  // Registry of the mounted canvases' imperative controls, so one toolbar can
  // drive every visible canvas (single, dual, or comparison grid).
  const handlesRef = useRef<Set<ModelViewHandle>>(new Set())
  const registerHandle = useCallback((handle: ModelViewHandle) => {
    handlesRef.current.add(handle)
  }, [])
  const unregisterHandle = useCallback((handle: ModelViewHandle) => {
    handlesRef.current.delete(handle)
  }, [])

  const resolvedModels = useMemo<ModelSource[]>(() => {
    // Priority 1: Explicit models prop (e.g. from parent passing comparison selection)
    if (models && models.length > 0) return capModelSelection(models)

    // Priority 2: Models generated from Pareto design selection (Req 4.1, 4.2).
    // When selectedIds are available from the DesignSelectionContext (Req 4.3),
    // filter to show only the models whose designs are currently selected in the
    // Results Dashboard. This drives the side-by-side comparison grid.
    if (generatedModels.length > 0) {
      // Filter by selected IDs when the context provides them (Req 4.3)
      const modelsToShow =
        contextSelectedIds.length > 0
          ? generatedModels.filter((gm) => contextSelectedIds.includes(gm.designId))
          : generatedModels

      if (modelsToShow.length > 0) {
        const sources: ModelSource[] = modelsToShow.map((gm) => ({
          id: `${gm.sessionId}/${gm.designId}`,
          stlUrl: gm.modelUrl,
          label: `Design ${gm.designId}`,
          metadata: {
            drawingId: gm.designId,
            title: `Generated from session ${gm.sessionId}`,
          },
        }))
        return capModelSelection(sources)
      }
    }

    // Priority 3: Derive from latest WebSocket result payload (legacy path)
    if (ws?.lastMessage?.type === 'result') {
      const payload = ws.lastMessage.payload as Record<string, unknown> | null
      // Skip model_generated frames here; they're handled in the effect above
      if (payload && payload.action !== 'model_generated') {
        return capModelSelection(deriveModelsFromPayload(ws.lastMessage.payload))
      }
    }
    return []
  }, [models, generatedModels, contextSelectedIds, ws?.lastMessage])

  const hasModels = resolvedModels.length > 0
  const showDualPanel = hasModels && resolvedModels.length === 1 && drawing !== null
  const primary = resolvedModels[0]

  // Derive loading/error design states from the modelStates prop (Req 4.4, 4.5).
  const generatingDesigns = useMemo(
    () => (modelStates ?? []).filter((s) => s.status === 'generating'),
    [modelStates],
  )
  const errorDesigns = useMemo(
    () => (modelStates ?? []).filter((s) => s.status === 'error'),
    [modelStates],
  )

  const resetView = () => handlesRef.current.forEach((h) => h.resetView())
  const setView = (view: 'front' | 'top' | 'right') =>
    handlesRef.current.forEach((h) => h.setView(view))
  const screenshot = () => {
    const first = handlesRef.current.values().next().value as
      | ModelViewHandle
      | undefined
    first?.screenshot(`${primary?.label ?? primary?.id ?? 'model'}.png`)
  }

  return (
    <section
      className="flex h-full w-full flex-col gap-3 rounded-lg border border-slate-200 bg-slate-50 p-4 shadow-sm"
      aria-labelledby="viewer-heading"
    >
      <header className="flex flex-col gap-1">
        <h1 id="viewer-heading" className="text-2xl font-semibold text-slate-900">
          Post-Processing
        </h1>
        <p className="text-sm text-slate-600">
          Inspect generated 3D models with orbit, zoom, and pan. Snap to camera
          presets, toggle wireframe or axes, slice a cross-section, capture a
          screenshot, and click annotations for full engineering detail. Compare
          up to {MAX_MODELS} designs side-by-side.
        </p>
      </header>

      {/* Controls bar (Req 6.2) */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-slate-200 bg-slate-100 px-3 py-2 text-sm">
        {/* Camera group */}
        <div className="flex items-center gap-1.5">
          <button type="button" onClick={resetView} disabled={!hasModels} className={TOOLBAR_BTN} title="Reset view">
            Reset
          </button>
          <button type="button" onClick={() => setView('front')} disabled={!hasModels} className={TOOLBAR_BTN} title="Front view">
            Front
          </button>
          <button type="button" onClick={() => setView('top')} disabled={!hasModels} className={TOOLBAR_BTN} title="Top view">
            Top
          </button>
          <button type="button" onClick={() => setView('right')} disabled={!hasModels} className={TOOLBAR_BTN} title="Right view">
            Right
          </button>
        </div>

        <span aria-hidden className="h-5 w-px bg-slate-300" />

        {/* Display toggles */}
        <label className="flex items-center gap-2 text-slate-700">
          <input
            type="checkbox"
            checked={wireframe}
            onChange={(e) => setWireframe(e.target.checked)}
            className="h-4 w-4"
          />
          Wireframe
        </label>
        <label className="flex items-center gap-2 text-slate-700">
          <input
            type="checkbox"
            checked={axes}
            onChange={(e) => setAxes(e.target.checked)}
            className="h-4 w-4"
          />
          Axes
        </label>
        <label className="flex items-center gap-2 text-slate-700">
          <input
            type="checkbox"
            checked={clipEnabled}
            onChange={(e) => setClipEnabled(e.target.checked)}
            className="h-4 w-4"
          />
          Cross-section
        </label>
        <label className="flex min-w-[8rem] flex-1 items-center gap-2 text-slate-700">
          <span className="whitespace-nowrap text-slate-500">Plane</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={clipOffset}
            disabled={!clipEnabled}
            onChange={(e) => setClipOffset(Number(e.target.value))}
            className="w-full max-w-xs accent-sky-600 disabled:opacity-40"
            aria-label="Cross-section plane offset"
          />
        </label>

        <span aria-hidden className="h-5 w-px bg-slate-300" />

        <button type="button" onClick={screenshot} disabled={!hasModels} className={TOOLBAR_BTN} title="Download PNG screenshot">
          Screenshot
        </button>
      </div>

      {/* Body */}
      {!hasModels ? (
        /* Loading state: show spinner when one or more designs are generating (Req 4.4) */
        generatingDesigns.length > 0 ? (
          <div
            className="flex min-h-0 flex-1 flex-col items-center justify-center rounded-md border border-sky-200 bg-sky-50/50 p-8 text-center"
            role="status"
            aria-live="polite"
          >
            <svg
              className="h-8 w-8 animate-spin text-sky-600"
              viewBox="0 0 24 24"
              fill="none"
              aria-hidden="true"
            >
              <circle
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="2"
                strokeOpacity="0.25"
              />
              <path
                d="M12 2a10 10 0 019.9 8.5"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
            <p className="mt-3 text-sm font-medium text-sky-800">
              Generating 3D model{generatingDesigns.length > 1 ? 's' : ''}…
            </p>
            <p className="mt-1 text-xs text-sky-600">
              {generatingDesigns.map((s) => s.designId).join(', ')}
            </p>
          </div>
        ) : /* Error state: show error with design ID and retry when model failed (Req 4.5) */
        errorDesigns.length > 0 ? (
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center rounded-md border border-rose-200 bg-rose-50/50 p-8 text-center">
            <svg
              className="h-8 w-8 text-rose-500"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="10" />
              <path d="M12 8v4m0 4h.01" strokeLinecap="round" />
            </svg>
            <p className="mt-3 text-sm font-medium text-rose-800">
              Failed to load 3D model{errorDesigns.length > 1 ? 's' : ''}
            </p>
            <ul className="mt-2 space-y-1">
              {errorDesigns.map((s) => (
                <li key={s.designId} className="flex items-center justify-center gap-2 text-xs text-rose-700">
                  <span className="font-mono">{s.designId}</span>
                  {s.error && <span className="text-rose-500">— {s.error}</span>}
                  {retryGeneration && (
                    <button
                      type="button"
                      onClick={() => retryGeneration(s.designId)}
                      className="rounded bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-800 hover:bg-rose-200 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-rose-500"
                      aria-label={`Retry generation for design ${s.designId}`}
                    >
                      Retry
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ) : (
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center rounded-md border border-dashed border-slate-300 bg-slate-50 p-8 text-center">
          <p className="text-base font-medium text-slate-700">No model to display</p>
          <p className="mt-1 max-w-md text-sm text-slate-500">
            Generate a 3D model from a design or optimization run. The STL will load
            here automatically and appear within a few seconds.
          </p>
        </div>
        )
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[minmax(0,300px)_minmax(0,1fr)]">
          {/* Spec sidebar (parity with original info panel) */}
          <div className="min-h-0">
            <SpecSidebar metadata={primary?.metadata} />
          </div>

          {/* Canvas area: dual-panel (drawing + model) or comparison grid */}
          {showDualPanel ? (
            <div className="grid min-h-0 grid-cols-1 gap-3 lg:grid-cols-2">
              <DrawingPane drawing={drawing!} />
              <div className="min-h-0 overflow-hidden rounded-md border border-slate-200">
                <ModelCanvas
                  stlUrl={resolvedModels[0].stlUrl}
                  wireframe={wireframe}
                  clipEnabled={clipEnabled}
                  clipOffset={clipOffset}
                  axes={axes}
                  annotations={resolvedModels[0].annotations ?? NO_ANNOTATIONS}
                  registerHandle={registerHandle}
                  unregisterHandle={unregisterHandle}
                />
              </div>
            </div>
          ) : (
            <div className={`grid min-h-0 gap-3 ${gridClass(resolvedModels.length)}`}>
              {resolvedModels.map((model) => (
                <figure
                  key={model.id}
                  className="flex min-h-0 flex-col overflow-hidden rounded-md border border-slate-200"
                >
                  <figcaption className="border-b border-slate-200 bg-slate-100 px-3 py-1.5 text-xs font-semibold text-slate-600">
                    {model.label ?? model.id}
                  </figcaption>
                  <div className="min-h-0 flex-1">
                    <ModelCanvas
                      stlUrl={model.stlUrl}
                      wireframe={wireframe}
                      clipEnabled={clipEnabled}
                      clipOffset={clipOffset}
                      axes={axes}
                      annotations={model.annotations ?? NO_ANNOTATIONS}
                      registerHandle={registerHandle}
                      unregisterHandle={unregisterHandle}
                    />
                  </div>
                </figure>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

export default ViewerPanel
