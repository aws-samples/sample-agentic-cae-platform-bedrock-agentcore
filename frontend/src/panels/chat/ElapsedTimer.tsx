import { useEffect, useRef, useState } from 'react'

interface ElapsedTimerProps {
  /** Extra classes for the wrapping <span>. */
  className?: string
  /**
   * Optional label shown before the time (e.g. "elapsed"). When omitted the
   * timer renders just the formatted duration.
   */
  label?: string
}

/** Format a whole-second duration as `Ss` (< 1 min) or `Mm SSs`. */
export function formatElapsed(totalSeconds: number): string {
  if (totalSeconds < 60) {
    return `${totalSeconds}s`
  }
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}

/**
 * A live counter that shows the wall-clock time elapsed since the component
 * mounted, updating every second. Used alongside the "Crew is on it…" thinking
 * indicator and the optimization ProgressIndicator so long-running operations
 * (e.g. a ~30s NSGA-II run) show visible, reassuring progress.
 *
 * The timer resets whenever it is unmounted/remounted, so it naturally starts
 * at 0 each time an operation begins.
 */
function ElapsedTimer({ className, label }: ElapsedTimerProps) {
  const [seconds, setSeconds] = useState(0)
  const startRef = useRef<number>(Date.now())

  useEffect(() => {
    startRef.current = Date.now()
    setSeconds(0)
    const id = window.setInterval(() => {
      setSeconds(Math.floor((Date.now() - startRef.current) / 1000))
    }, 1000)
    return () => window.clearInterval(id)
  }, [])

  return (
    <span className={className} aria-label={`Elapsed time ${formatElapsed(seconds)}`}>
      {label ? `${label} ` : ''}
      <span className="tabular-nums">{formatElapsed(seconds)}</span>
    </span>
  )
}

export default ElapsedTimer
