import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import {
  DEFAULT_BACKOFF_SCHEDULE,
  MAX_RECONNECT_ATTEMPTS,
  backoffDelayForAttempt,
} from './backoff'

const EXPECTED_SCHEDULE = [1000, 2000, 4000, 8000, 15000]

describe('exponential backoff schedule', () => {
  it('matches the design schedule (1, 2, 4, 8, 15s; 5 attempts, ~30s window)', () => {
    expect([...DEFAULT_BACKOFF_SCHEDULE]).toEqual(EXPECTED_SCHEDULE)
    expect(MAX_RECONNECT_ATTEMPTS).toBe(5)
    const total = DEFAULT_BACKOFF_SCHEDULE.reduce((a, b) => a + b, 0)
    expect(total).toBeLessThanOrEqual(31000)
  })

  it('property: delays for attempts 1..5 equal the schedule prefix', () => {
    fc.assert(
      fc.property(fc.integer({ min: 1, max: 5 }), (n) => {
        const delays = Array.from({ length: n }, (_, i) =>
          backoffDelayForAttempt(i + 1),
        )
        expect(delays).toEqual(EXPECTED_SCHEDULE.slice(0, n))
        const total = delays.reduce<number>((a, b) => a + (b ?? 0), 0)
        // Any prefix of the schedule stays within the ~30s reconnection window.
        expect(total).toBeLessThanOrEqual(31000)
      }),
    )
  })

  it('property: attempts beyond the cap yield null (reconnection stops)', () => {
    fc.assert(
      fc.property(fc.integer({ min: 6, max: 1000 }), (n) => {
        expect(backoffDelayForAttempt(n)).toBeNull()
      }),
    )
  })

  it('property: non-positive / non-integer attempts yield null', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.integer({ min: -100, max: 0 }),
          fc.double({ min: 1.1, max: 4.9, noNaN: true }).filter((x) => !Number.isInteger(x)),
        ),
        (n) => {
          expect(backoffDelayForAttempt(n)).toBeNull()
        },
      ),
    )
  })
})
