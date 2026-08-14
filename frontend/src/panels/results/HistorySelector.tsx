/**
 * History Selector component (Req 5.2, 5.4): renders a dropdown of optimization
 * runs for the active session, showing timestamp, design count, and source
 * drawing ID for each run. Supports a loading state while history is being
 * fetched from the Session Detail API.
 */

export interface OptimizationRunSummary {
  /** ISO-8601 timestamp of when the optimization run completed. */
  timestamp: string
  /** Number of Pareto designs produced in this run. */
  designCount: number
  /** Source drawing identifier (if available). */
  sourceDrawingId?: string
  /** Number of NSGA-II generations executed in this run. */
  generationCount: number
}

export interface HistorySelectorProps {
  /** Summary array of optimization runs (most recent first). */
  runs: readonly OptimizationRunSummary[]
  /** Index of the currently selected run. */
  selectedIndex: number
  /** Callback when the user picks a different run. */
  onSelect: (index: number) => void
  /** Show a loading spinner when history is being fetched. */
  loading?: boolean
  /** Resolve a drawing ID to a human-readable name (filename). */
  resolveDrawingName?: (drawingId: string) => string
}

/** Format an ISO-8601 timestamp into a concise locale string. */
function formatTimestamp(iso: string): string {
  try {
    const date = new Date(iso)
    return date.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function HistorySelector({ runs, selectedIndex, onSelect, loading, resolveDrawingName }: HistorySelectorProps) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-slate-500" role="status" aria-live="polite">
        <svg
          className="h-4 w-4 animate-spin text-sky-600"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
          />
        </svg>
        <span>Loading history…</span>
      </div>
    )
  }

  if (runs.length === 0) {
    return null
  }

  return (
    <div className="flex items-center gap-2">
      <label
        htmlFor="history-selector"
        className="text-xs font-medium uppercase tracking-wide text-slate-500"
      >
        Run
      </label>
      <select
        id="history-selector"
        value={selectedIndex}
        onChange={(e) => onSelect(Number(e.target.value))}
        className="rounded border border-slate-300 bg-white px-2 py-1 text-sm text-slate-700 shadow-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
        aria-label="Select optimization run"
      >
        {runs.map((run, index) => (
          <option key={`${run.timestamp}-${index}`} value={index}>
            {formatTimestamp(run.timestamp)} — {run.designCount} design{run.designCount !== 1 ? 's' : ''}
            {run.sourceDrawingId ? ` (${resolveDrawingName ? resolveDrawingName(run.sourceDrawingId) : run.sourceDrawingId})` : ''}
          </option>
        ))}
      </select>
      <span className="text-xs text-slate-400">
        {runs.length} run{runs.length !== 1 ? 's' : ''}
      </span>
    </div>
  )
}

export default HistorySelector
