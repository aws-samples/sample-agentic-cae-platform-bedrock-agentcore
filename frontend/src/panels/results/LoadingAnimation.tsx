import ElapsedTimer from '../chat/ElapsedTimer'

interface LoadingAnimationProps {
  /** Current NSGA-II generation, if reported. */
  generation?: number
  /** Current hypervolume indicator, if reported. */
  hypervolume?: number
  /** Estimated time remaining in seconds, if reported. */
  etaSeconds?: number
}

/** Format an ETA in seconds as a compact human string. */
function formatEta(etaSeconds: number | undefined): string {
  if (etaSeconds === undefined) return '—'
  if (etaSeconds < 60) return `${Math.round(etaSeconds)}s`
  const minutes = Math.floor(etaSeconds / 60)
  const seconds = Math.round(etaSeconds % 60)
  return `${minutes}m ${seconds}s`
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col items-center">
      <span className="text-lg font-semibold tabular-nums text-slate-900">{value}</span>
      <span className="text-xs uppercase tracking-wide text-slate-500">{label}</span>
    </div>
  )
}

/**
 * Full-panel "Optimizing…" animation (parity with the original SimAgent
 * LoadingAnimation): pulsing concentric rings around a CPU glyph, bouncing
 * dots, and live NSGA-II counters (generation, hypervolume, ETA) driven by
 * `progress` frames.
 */
function LoadingAnimation({
  generation,
  hypervolume,
  etaSeconds,
}: LoadingAnimationProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex min-h-0 flex-1 flex-col items-center justify-center gap-6 rounded-md border border-slate-200 bg-slate-50 p-10 text-center"
    >
      <div className="relative flex h-28 w-28 items-center justify-center">
        {/* Pulsing concentric rings */}
        <span className="absolute inline-flex h-28 w-28 animate-ping rounded-full border-2 border-sky-400 opacity-60" />
        <span
          className="absolute inline-flex h-20 w-20 animate-ping rounded-full border-2 border-sky-500 opacity-70"
          style={{ animationDelay: '150ms' }}
        />
        <span className="absolute inline-flex h-14 w-14 rounded-full bg-sky-100" />
        {/* CPU glyph */}
        <svg
          className="relative h-8 w-8 text-sky-600"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <rect x="6" y="6" width="12" height="12" rx="1.5" />
          <rect x="9" y="9" width="6" height="6" rx="0.5" />
          <path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
        </svg>
      </div>

      <div className="flex flex-col items-center gap-2">
        <p className="flex items-center gap-1 text-base font-semibold text-slate-800">
          Optimizing
          <span className="inline-flex gap-0.5">
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sky-500 [animation-delay:-0.3s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sky-500 [animation-delay:-0.15s]" />
            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-sky-500" />
          </span>
        </p>
        <p className="max-w-sm text-sm text-slate-500">
          Running the NSGA-II multi-objective search over the design space. Live
          metrics update as generations complete.
        </p>
      </div>

      <div className="flex items-center gap-10">
        <Metric label="Generation" value={generation !== undefined ? String(generation) : '—'} />
        <Metric
          label="Hypervolume"
          value={hypervolume !== undefined ? hypervolume.toFixed(4) : '—'}
        />
        <Metric label="ETA" value={formatEta(etaSeconds)} />
        <div className="flex flex-col items-center">
          <ElapsedTimer className="text-lg font-semibold tabular-nums text-slate-900" />
          <span className="text-xs uppercase tracking-wide text-slate-500">Elapsed</span>
        </div>
      </div>
    </div>
  )
}

export default LoadingAnimation
