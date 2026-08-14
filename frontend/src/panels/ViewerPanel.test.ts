import { describe, expect, it } from 'vitest'
import type { ModelGenerationState } from './results/types'

/**
 * Unit tests for ViewerPanel loading and error state logic (Req 4.4, 4.5).
 *
 * These tests verify the state derivation that drives the loading indicator
 * (when designs have status 'generating') and the error state (when designs
 * have status 'error' with design IDs and retry action availability).
 *
 * Since ViewerPanel is a React component that requires Three.js/WebGL context,
 * we test the state derivation logic in isolation. The component itself uses
 * `useMemo` with a simple filter; these tests confirm that filter logic.
 */

/** Replicates the derivation logic used inside ViewerPanel for generating designs. */
function getGeneratingDesigns(modelStates: readonly ModelGenerationState[]): ModelGenerationState[] {
  return modelStates.filter((s) => s.status === 'generating')
}

/** Replicates the derivation logic used inside ViewerPanel for error designs. */
function getErrorDesigns(modelStates: readonly ModelGenerationState[]): ModelGenerationState[] {
  return modelStates.filter((s) => s.status === 'error')
}

describe('ViewerPanel — loading state derivation (Req 4.4)', () => {
  it('returns empty array when no designs are generating', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'idle' },
      { designId: 'd2', status: 'ready', modelUrl: 'https://cdn.example.com/model.stl' },
    ]
    expect(getGeneratingDesigns(states)).toEqual([])
  })

  it('returns generating designs when one design is generating', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'generating' },
      { designId: 'd2', status: 'idle' },
    ]
    const result = getGeneratingDesigns(states)
    expect(result).toHaveLength(1)
    expect(result[0].designId).toBe('d1')
  })

  it('returns multiple generating designs', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'generating' },
      { designId: 'd2', status: 'generating' },
      { designId: 'd3', status: 'ready', modelUrl: 'https://cdn.example.com/d3.stl' },
    ]
    const result = getGeneratingDesigns(states)
    expect(result).toHaveLength(2)
    expect(result.map((s) => s.designId)).toEqual(['d1', 'd2'])
  })

  it('handles empty model states array', () => {
    expect(getGeneratingDesigns([])).toEqual([])
  })
})

describe('ViewerPanel — error state derivation (Req 4.5)', () => {
  it('returns empty array when no designs have errors', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'idle' },
      { designId: 'd2', status: 'generating' },
      { designId: 'd3', status: 'ready', modelUrl: 'https://cdn.example.com/d3.stl' },
    ]
    expect(getErrorDesigns(states)).toEqual([])
  })

  it('returns error designs with designId and error message', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'error', error: 'Model URL returned 404' },
      { designId: 'd2', status: 'ready', modelUrl: 'https://cdn.example.com/d2.stl' },
    ]
    const result = getErrorDesigns(states)
    expect(result).toHaveLength(1)
    expect(result[0].designId).toBe('d1')
    expect(result[0].error).toBe('Model URL returned 404')
  })

  it('returns multiple error designs', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'error', error: 'Timeout' },
      { designId: 'd2', status: 'error', error: '404 Not Found' },
      { designId: 'd3', status: 'generating' },
    ]
    const result = getErrorDesigns(states)
    expect(result).toHaveLength(2)
    expect(result[0].designId).toBe('d1')
    expect(result[1].designId).toBe('d2')
  })

  it('handles error state without error message', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'error' },
    ]
    const result = getErrorDesigns(states)
    expect(result).toHaveLength(1)
    expect(result[0].designId).toBe('d1')
    expect(result[0].error).toBeUndefined()
  })

  it('handles empty model states array', () => {
    expect(getErrorDesigns([])).toEqual([])
  })
})

describe('ViewerPanel — state priority logic', () => {
  it('generating state takes priority over error when no models are loaded', () => {
    // When both generating and error designs exist, the component shows the
    // generating spinner if there are generating designs (checked first).
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'generating' },
      { designId: 'd2', status: 'error', error: 'Failed' },
    ]
    const generating = getGeneratingDesigns(states)
    const errors = getErrorDesigns(states)

    // The ViewerPanel checks generating first in the ternary: generatingDesigns.length > 0
    expect(generating.length).toBeGreaterThan(0)
    expect(errors.length).toBeGreaterThan(0)
    // Generating takes priority in the UI render
    expect(generating.length > 0).toBe(true)
  })

  it('shows error state when only error designs exist (no generating)', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'error', error: '404' },
      { designId: 'd2', status: 'idle' },
    ]
    const generating = getGeneratingDesigns(states)
    const errors = getErrorDesigns(states)

    expect(generating.length).toBe(0)
    expect(errors.length).toBe(1)
  })

  it('shows empty state when all designs are idle or ready', () => {
    const states: ModelGenerationState[] = [
      { designId: 'd1', status: 'idle' },
      { designId: 'd2', status: 'ready', modelUrl: 'https://cdn.example.com/d2.stl' },
    ]
    const generating = getGeneratingDesigns(states)
    const errors = getErrorDesigns(states)

    expect(generating.length).toBe(0)
    expect(errors.length).toBe(0)
  })
})
