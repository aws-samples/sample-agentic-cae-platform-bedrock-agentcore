import type { OptimizationResult } from './types'

/**
 * Illustrative optimization result used to render the dashboard in isolation
 * (before the WebSocket `result` frame wiring lands). Two objectives — thermal
 * resistance and mass — with per-constraint pass/fail status and a short
 * hypervolume convergence series.
 */
export const SAMPLE_RESULT: OptimizationResult = {
  populationSize: 100,
  generationCount: 6,
  sourceDrawingId: 'DRW-1042',
  extractionId: 'EXT-88f3',
  designs: [
    {
      id: 'D1',
      objectives: { thermalResistance: 0.42, mass: 1.85 },
      constraints: [
        { name: 'max_temp <= 85C', passed: true, detail: '78.2C' },
        { name: 'fin_thickness >= 1.0mm', passed: true, detail: '1.4mm' },
      ],
      rationale: 'Lowest thermal resistance; heaviest of the set.',
    },
    {
      id: 'D2',
      objectives: { thermalResistance: 0.48, mass: 1.52 },
      constraints: [
        { name: 'max_temp <= 85C', passed: true, detail: '81.0C' },
        { name: 'fin_thickness >= 1.0mm', passed: true, detail: '1.1mm' },
      ],
      rationale: 'Balanced trade-off between cooling and weight.',
    },
    {
      id: 'D3',
      objectives: { thermalResistance: 0.55, mass: 1.21 },
      constraints: [
        { name: 'max_temp <= 85C', passed: true, detail: '84.1C' },
        { name: 'fin_thickness >= 1.0mm', passed: false, detail: '0.9mm' },
      ],
      rationale: 'Lightest design; fin thickness marginally violated.',
    },
    {
      id: 'D4',
      objectives: { thermalResistance: 0.5, mass: 1.4 },
      constraints: [
        { name: 'max_temp <= 85C', passed: true, detail: '82.5C' },
        { name: 'fin_thickness >= 1.0mm', passed: true, detail: '1.2mm' },
      ],
      rationale: 'Compromise design near the knee of the front.',
    },
    {
      id: 'D5',
      objectives: { thermalResistance: 0.6, mass: 1.05 },
      constraints: [
        { name: 'max_temp <= 85C', passed: false, detail: '86.4C' },
        { name: 'fin_thickness >= 1.0mm', passed: true, detail: '1.0mm' },
      ],
      rationale: 'Minimal mass; exceeds the temperature limit.',
    },
  ],
  convergence: [
    { generation: 1, hypervolume: 0.41 },
    { generation: 2, hypervolume: 0.58 },
    { generation: 3, hypervolume: 0.67 },
    { generation: 4, hypervolume: 0.72 },
    { generation: 5, hypervolume: 0.75 },
    { generation: 6, hypervolume: 0.76 },
  ],
}
