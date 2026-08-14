import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import { MAX_SESSIONS, type SessionSummary } from '../api/sessions'
import { formatLastAccess, formatMessageCount, sortSessionsByRecency } from './helpers'

function summary(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    sessionId: 's-1',
    title: 'Session',
    lastAccessAt: '2024-01-01T00:00:00.000Z',
    messageCount: 0,
    ...overrides,
  }
}

describe('sortSessionsByRecency (Req 13.2)', () => {
  it('orders sessions by last-access, most recent first', () => {
    const sorted = sortSessionsByRecency([
      summary({ sessionId: 'old', lastAccessAt: '2024-01-01T00:00:00.000Z' }),
      summary({ sessionId: 'new', lastAccessAt: '2024-03-01T00:00:00.000Z' }),
      summary({ sessionId: 'mid', lastAccessAt: '2024-02-01T00:00:00.000Z' }),
    ])
    expect(sorted.map((s) => s.sessionId)).toEqual(['new', 'mid', 'old'])
  })

  it('caps the list at MAX_SESSIONS (50)', () => {
    const many = Array.from({ length: MAX_SESSIONS + 20 }, (_, i) =>
      summary({ sessionId: `s-${i}`, lastAccessAt: new Date(1_700_000_000_000 + i * 1000).toISOString() }),
    )
    expect(sortSessionsByRecency(many)).toHaveLength(MAX_SESSIONS)
  })

  it('does not mutate the input array', () => {
    const input = [
      summary({ sessionId: 'a', lastAccessAt: '2024-01-01T00:00:00.000Z' }),
      summary({ sessionId: 'b', lastAccessAt: '2024-02-01T00:00:00.000Z' }),
    ]
    const before = input.map((s) => s.sessionId)
    sortSessionsByRecency(input)
    expect(input.map((s) => s.sessionId)).toEqual(before)
  })

  it('property: result is descending by last-access and length <= 50', () => {
    const summaryArb = fc
      .record({
        id: fc.string({ minLength: 1, maxLength: 8 }),
        ms: fc.integer({ min: 0, max: 4_000_000_000_000 }),
        messageCount: fc.nat({ max: 1000 }),
      })
      .map(({ id, ms, messageCount }) =>
        summary({ sessionId: id, lastAccessAt: new Date(ms).toISOString(), messageCount }),
      )

    fc.assert(
      fc.property(fc.array(summaryArb, { maxLength: 120 }), (sessions) => {
        const sorted = sortSessionsByRecency(sessions)
        // Length is capped at MAX_SESSIONS.
        expect(sorted.length).toBeLessThanOrEqual(MAX_SESSIONS)
        expect(sorted.length).toBeLessThanOrEqual(sessions.length)
        // Each element's access time is >= the next (descending order).
        for (let i = 1; i < sorted.length; i += 1) {
          const prev = Date.parse(sorted[i - 1].lastAccessAt)
          const curr = Date.parse(sorted[i].lastAccessAt)
          expect(prev).toBeGreaterThanOrEqual(curr)
        }
      }),
    )
  })
})

describe('formatLastAccess (Req 13.6)', () => {
  it('returns a non-empty string for a valid ISO timestamp', () => {
    expect(formatLastAccess('2024-03-01T12:30:00.000Z').length).toBeGreaterThan(0)
  })

  it('returns Unknown for an invalid timestamp', () => {
    expect(formatLastAccess('not-a-date')).toBe('Unknown')
    expect(formatLastAccess('')).toBe('Unknown')
  })
})

describe('formatMessageCount (Req 13.6)', () => {
  it('pluralizes correctly', () => {
    expect(formatMessageCount(0)).toBe('0 messages')
    expect(formatMessageCount(1)).toBe('1 message')
    expect(formatMessageCount(5)).toBe('5 messages')
  })

  it('coerces negative or non-finite counts to 0', () => {
    expect(formatMessageCount(-3)).toBe('0 messages')
    expect(formatMessageCount(Number.NaN)).toBe('0 messages')
  })
})
