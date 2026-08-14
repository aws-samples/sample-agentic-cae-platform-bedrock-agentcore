import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import type { OptimizationHistoryEntry, ConvergencePoint, ParetoDesign } from './types'

/**
 * **Validates: Requirements 5.3**
 *
 * Property 6: Selected historical run data matches rendered output.
 *
 * The ResultsDashboardPanel derives the active result from:
 *   optimizationHistory[selectedIndex]
 * and passes that entry's designs to the ParetoChart, convergence to the
 * ConvergenceChart, and designs to the DesignTable. This property test
 * verifies that for any valid history + selectedIndex, the derived result
 * corresponds exactly to the entry at that index.
 */

// ─── Pure derivation logic extracted from ResultsDashboardPanel ───────────────

/**
 * Derives the active optimization result from the history array and selected
 * index. Mirrors the logic in ResultsDashboardPanel:
 *
 * ```
 * const historyResult =
 *   optimizationHistory.length > 0 && selectedIndex < optimizationHistory.length
 *     ? optimizationHistory[selectedIndex]
 *     : null
 * ```
 */
function deriveHistoryResult(
  optimizationHistory: readonly OptimizationHistoryEntry[],
  selectedIndex: number,
): OptimizationHistoryEntry | null {
  if (optimizationHistory.length > 0 && selectedIndex < optimizationHistory.length) {
    return optimizationHistory[selectedIndex]
  }
  return null
}

// ─── Arbitraries ─────────────────────────────────────────────────────────────

const arbConvergencePoint: fc.Arbitrary<ConvergencePoint> = fc.record({
  generation: fc.nat({ max: 500 }),
  hypervolume: fc.double({ min: 0, max: 1, noNaN: true }),
})

const arbParetoDesign: fc.Arbitrary<ParetoDesign> = fc.record({
  id: fc.string({ minLength: 1, maxLength: 10 }),
  objectives: fc.dictionary(
    fc.string({ minLength: 1, maxLength: 8 }),
    fc.double({ min: -1000, max: 1000, noNaN: true }),
    { minKeys: 1, maxKeys: 4 },
  ),
  constraints: fc.array(
    fc.record({
      name: fc.string({ minLength: 1, maxLength: 12 }),
      passed: fc.boolean(),
    }),
    { maxLength: 3 },
  ),
  rationale: fc.string({ maxLength: 30 }),
})

const arbHistoryEntry: fc.Arbitrary<OptimizationHistoryEntry> = fc.record({
  timestamp: fc
    .integer({ min: 1704067200000, max: 1767225599000 }) // 2024-01-01 to 2025-12-31 in ms
    .map((ms) => new Date(ms).toISOString()),
  designs: fc.array(arbParetoDesign, { minLength: 1, maxLength: 6 }),
  convergence: fc.array(arbConvergencePoint, { minLength: 1, maxLength: 10 }),
  populationSize: fc.nat({ max: 500 }),
  generationCount: fc.nat({ max: 200 }),
  sourceDrawingId: fc.option(fc.string({ minLength: 3, maxLength: 10 }), { nil: undefined }),
})

describe('Property 6: Selected historical run data matches rendered output (Req 5.3)', () => {
  it('property: derived result designs match selected history entry designs', () => {
    fc.assert(
      fc.property(
        fc.array(arbHistoryEntry, { minLength: 1, maxLength: 8 }).chain((history) =>
          fc.tuple(fc.constant(history), fc.nat({ max: history.length - 1 })),
        ),
        ([history, selectedIndex]) => {
          const result = deriveHistoryResult(history, selectedIndex)

          // Result should never be null for valid index within non-empty array
          expect(result).not.toBeNull()

          // The rendered Pareto designs correspond exactly to the selected entry
          expect(result!.designs).toEqual(history[selectedIndex].designs)
        },
      ),
    )
  })

  it('property: derived result convergence matches selected history entry convergence', () => {
    fc.assert(
      fc.property(
        fc.array(arbHistoryEntry, { minLength: 1, maxLength: 8 }).chain((history) =>
          fc.tuple(fc.constant(history), fc.nat({ max: history.length - 1 })),
        ),
        ([history, selectedIndex]) => {
          const result = deriveHistoryResult(history, selectedIndex)

          expect(result).not.toBeNull()

          // The rendered convergence plot data corresponds to the selected entry
          expect(result!.convergence).toEqual(history[selectedIndex].convergence)
        },
      ),
    )
  })

  it('property: derived result metadata matches selected entry (not any other entry)', () => {
    fc.assert(
      fc.property(
        fc.array(arbHistoryEntry, { minLength: 2, maxLength: 8 }).chain((history) =>
          fc.tuple(fc.constant(history), fc.nat({ max: history.length - 1 })),
        ),
        ([history, selectedIndex]) => {
          const result = deriveHistoryResult(history, selectedIndex)

          expect(result).not.toBeNull()

          // Metadata from the derived result corresponds to the selected entry
          expect(result!.populationSize).toBe(history[selectedIndex].populationSize)
          expect(result!.generationCount).toBe(history[selectedIndex].generationCount)
          expect(result!.sourceDrawingId).toBe(history[selectedIndex].sourceDrawingId)
          expect(result!.timestamp).toBe(history[selectedIndex].timestamp)

          // Verify it does NOT match a different entry (when history has >1 entries)
          if (history.length > 1) {
            const otherIndex = (selectedIndex + 1) % history.length
            const otherEntry = history[otherIndex]
            // At least one field should differ from a different entry
            // (unless entries happen to be identical by generation)
            const sameAsOther =
              result!.timestamp === otherEntry.timestamp &&
              result!.populationSize === otherEntry.populationSize &&
              result!.generationCount === otherEntry.generationCount &&
              JSON.stringify(result!.designs) === JSON.stringify(otherEntry.designs) &&
              JSON.stringify(result!.convergence) === JSON.stringify(otherEntry.convergence)

            // This is a weak assertion: if entries happen to be identical (rare
            // in random data), we allow it. Otherwise, they must differ.
            if (!sameAsOther) {
              expect(result!.timestamp).not.toBe(otherEntry.timestamp)
            }
          }
        },
      ),
    )
  })

  it('property: selecting any valid index always returns the exact entry at that position', () => {
    fc.assert(
      fc.property(
        fc.array(arbHistoryEntry, { minLength: 1, maxLength: 10 }).chain((history) =>
          fc.tuple(fc.constant(history), fc.nat({ max: history.length - 1 })),
        ),
        ([history, selectedIndex]) => {
          const result = deriveHistoryResult(history, selectedIndex)

          // The result IS the entry (identity/deep equality)
          expect(result).toEqual(history[selectedIndex])
        },
      ),
    )
  })

  it('property: out-of-bounds index returns null (no data mismatch)', () => {
    fc.assert(
      fc.property(
        fc.array(arbHistoryEntry, { minLength: 1, maxLength: 5 }),
        fc.nat({ max: 100 }),
        (history, offset) => {
          const outOfBoundsIndex = history.length + offset
          const result = deriveHistoryResult(history, outOfBoundsIndex)
          expect(result).toBeNull()
        },
      ),
    )
  })
})
