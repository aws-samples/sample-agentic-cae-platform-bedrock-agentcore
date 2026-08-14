/**
 * Exponential backoff schedule for WebSocket reconnection.
 *
 * The schedule is fixed by the design (§Exponential Backoff Strategy):
 *
 *   Attempt 1: 1s   Attempt 2: 2s   Attempt 3: 4s   Attempt 4: 8s
 *   Attempt 5: 15s (capped)   Total window: ~30s
 *
 * After the last attempt the client transitions to a `failed` status and
 * surfaces a manual reconnect action (Req 12.4, 12.5).
 */
export const DEFAULT_BACKOFF_SCHEDULE: readonly number[] = [
  1000, 2000, 4000, 8000, 15000,
]

/** Maximum reconnection attempts before giving up (Req 12.4). */
export const MAX_RECONNECT_ATTEMPTS = DEFAULT_BACKOFF_SCHEDULE.length

/**
 * Returns the delay (ms) to wait before a given 1-based reconnection attempt,
 * or `null` if the attempt is out of range (i.e. reconnection should stop and
 * the client should transition to `failed`).
 *
 * @param attempt 1-based attempt number (1 = first reconnection)
 * @param schedule backoff schedule to use (defaults to the design schedule)
 */
export function backoffDelayForAttempt(
  attempt: number,
  schedule: readonly number[] = DEFAULT_BACKOFF_SCHEDULE,
): number | null {
  if (!Number.isInteger(attempt) || attempt < 1 || attempt > schedule.length) {
    return null
  }
  return schedule[attempt - 1]
}
