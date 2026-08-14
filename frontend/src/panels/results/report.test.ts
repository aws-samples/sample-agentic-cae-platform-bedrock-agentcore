import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import { buildReportMarkdown } from './report'
import type { OptimizationResult } from './types'

const RESULT: OptimizationResult = {
  populationSize: 100,
  generationCount: 6,
  sourceDrawingId: 'DRW-1042',
  extractionId: 'EXT-88f3',
  designs: [
    {
      id: 'D1',
      objectives: { thermalResistance: 0.42, mass: 1.85 },
      constraints: [
        { name: 'max_temp <= 85C', passed: true },
        { name: 'fin_thickness >= 1.0mm', passed: true },
      ],
      rationale: 'Lowest thermal resistance.',
    },
    {
      id: 'D2',
      objectives: { thermalResistance: 0.55, mass: 1.21 },
      constraints: [
        { name: 'max_temp <= 85C', passed: true },
        { name: 'fin_thickness >= 1.0mm', passed: false },
      ],
      rationale: 'Lightest design; fin thickness violated.',
    },
  ],
  convergence: [
    { generation: 1, hypervolume: 0.41 },
    { generation: 6, hypervolume: 0.76 },
  ],
}

describe('buildReportMarkdown (Req 5.6 — report completeness)', () => {
  const markdown = buildReportMarkdown(RESULT)

  it('includes all required sections', () => {
    expect(markdown).toContain('# Optimization Report')
    expect(markdown).toContain('## Objectives')
    expect(markdown).toContain('## Constraints')
    expect(markdown).toContain('## Convergence Metrics')
    expect(markdown).toContain('## Pareto Set')
  })

  it('reports population size and generation count', () => {
    expect(markdown).toContain('Population size:** 100')
    expect(markdown).toContain('Generation count:** 6')
  })

  it('lists objectives and constraints from the designs', () => {
    expect(markdown).toContain('thermalResistance')
    expect(markdown).toContain('mass')
    expect(markdown).toContain('max_temp <= 85C')
    expect(markdown).toContain('fin_thickness >= 1.0mm')
  })

  it('references the source drawing for constraints (Req 5.6)', () => {
    expect(markdown).toContain('source drawing DRW-1042')
  })

  it('includes convergence metrics with initial and final hypervolume', () => {
    expect(markdown).toContain('Initial hypervolume (generation 1): 0.41')
    expect(markdown).toContain('Final hypervolume (generation 6): 0.76')
  })

  it('enumerates each Pareto design with objectives, constraints, and rationale', () => {
    expect(markdown).toContain('### Design D1')
    expect(markdown).toContain('### Design D2')
    expect(markdown).toContain('thermalResistance = 0.42')
    expect(markdown).toContain('max_temp <= 85C: PASS')
    expect(markdown).toContain('fin_thickness >= 1.0mm: FAIL')
    expect(markdown).toContain('Lightest design; fin thickness violated.')
  })

  it('handles an empty result without throwing and still emits every section', () => {
    const empty: OptimizationResult = {
      populationSize: 0,
      generationCount: 0,
      designs: [],
      convergence: [],
    }
    const emptyMarkdown = buildReportMarkdown(empty)
    expect(emptyMarkdown).toContain('## Objectives')
    expect(emptyMarkdown).toContain('## Constraints')
    expect(emptyMarkdown).toContain('## Convergence Metrics')
    expect(emptyMarkdown).toContain('## Pareto Set')
    expect(emptyMarkdown).toContain('_No Pareto-optimal designs found._')
  })

  it('property: every section header is always present regardless of contents', () => {
    const arbConstraint = fc.record({
      name: fc.string({ minLength: 1, maxLength: 8 }),
      passed: fc.boolean(),
    })
    const arbDesign = fc.record({
      id: fc.string({ minLength: 1, maxLength: 6 }),
      objectives: fc.dictionary(
        fc.string({ minLength: 1, maxLength: 6 }),
        fc.double({ min: -1000, max: 1000, noNaN: true }),
        { maxKeys: 4 },
      ),
      constraints: fc.array(arbConstraint, { maxLength: 4 }),
      rationale: fc.string({ maxLength: 20 }),
    })
    const arbResult = fc.record({
      populationSize: fc.nat({ max: 10000 }),
      generationCount: fc.nat({ max: 500 }),
      designs: fc.array(arbDesign, { maxLength: 6 }),
      convergence: fc.array(
        fc.record({
          generation: fc.nat({ max: 500 }),
          hypervolume: fc.double({ min: 0, max: 1, noNaN: true }),
        }),
        { maxLength: 8 },
      ),
    })

    fc.assert(
      fc.property(arbResult, (result) => {
        const md = buildReportMarkdown(result as OptimizationResult)
        expect(md).toContain('# Optimization Report')
        expect(md).toContain('## Objectives')
        expect(md).toContain('## Constraints')
        expect(md).toContain('## Convergence Metrics')
        expect(md).toContain('## Pareto Set')
        expect(md).toContain(`Population size:** ${result.populationSize}`)
        expect(md).toContain(`Generation count:** ${result.generationCount}`)
      }),
    )
  })
})
