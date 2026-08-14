/**
 * Unit tests for design selection filtering logic used by ViewerPanel (Req 4.3).
 * Tests the pure filtering behaviour: when selectedIds are provided, only
 * generated models whose designId matches are included in the comparison grid.
 */
import { describe, expect, it } from 'vitest'
import { capModelSelection } from '../viewer/geometry'
import type { ModelSource } from '../viewer/types'

interface GeneratedModel {
  sessionId: string
  designId: string
  modelUrl: string
}

/**
 * Replicates the filtering logic from ViewerPanel.resolvedModels (Priority 2):
 * when contextSelectedIds is non-empty, only models whose designId is in the
 * selection are shown; otherwise all generated models are shown.
 */
function resolveModelsFromGenerated(
  generatedModels: GeneratedModel[],
  contextSelectedIds: string[],
): ModelSource[] {
  if (generatedModels.length === 0) return []

  const modelsToShow =
    contextSelectedIds.length > 0
      ? generatedModels.filter((gm) => contextSelectedIds.includes(gm.designId))
      : generatedModels

  if (modelsToShow.length === 0) return []

  const sources: ModelSource[] = modelsToShow.map((gm) => ({
    id: `${gm.sessionId}/${gm.designId}`,
    stlUrl: gm.modelUrl,
    label: `Design ${gm.designId}`,
    metadata: {
      drawingId: gm.designId,
      title: `Generated from session ${gm.sessionId}`,
    },
  }))
  return capModelSelection(sources)
}

describe('Side-by-side comparison filtering (Req 4.3)', () => {
  const allModels: GeneratedModel[] = [
    { sessionId: 'sess-1', designId: 'D1', modelUrl: 'https://cdn/d1.stl' },
    { sessionId: 'sess-1', designId: 'D2', modelUrl: 'https://cdn/d2.stl' },
    { sessionId: 'sess-1', designId: 'D3', modelUrl: 'https://cdn/d3.stl' },
    { sessionId: 'sess-1', designId: 'D4', modelUrl: 'https://cdn/d4.stl' },
    { sessionId: 'sess-1', designId: 'D5', modelUrl: 'https://cdn/d5.stl' },
  ]

  it('shows all generated models when no selection is active', () => {
    const result = resolveModelsFromGenerated(allModels, [])
    // capModelSelection caps at 4
    expect(result).toHaveLength(4)
  })

  it('filters to only selected designs when selection is active', () => {
    const result = resolveModelsFromGenerated(allModels, ['D2', 'D4'])
    expect(result).toHaveLength(2)
    expect(result[0].id).toBe('sess-1/D2')
    expect(result[1].id).toBe('sess-1/D4')
  })

  it('handles selection of designs not yet generated (returns empty subset)', () => {
    const result = resolveModelsFromGenerated(allModels, ['D99'])
    expect(result).toHaveLength(0)
  })

  it('caps at MAX_MODELS (4) even when more selected designs are ready', () => {
    const result = resolveModelsFromGenerated(allModels, ['D1', 'D2', 'D3', 'D4', 'D5'])
    expect(result).toHaveLength(4)
  })

  it('preserves source array order in the comparison grid', () => {
    const result = resolveModelsFromGenerated(allModels, ['D3', 'D1'])
    expect(result).toHaveLength(2)
    // Filter preserves the order models appear in generatedModels (D1 before D3)
    expect(result[0].id).toBe('sess-1/D1')
    expect(result[1].id).toBe('sess-1/D3')
  })

  it('returns empty array when no models are generated', () => {
    const result = resolveModelsFromGenerated([], ['D1', 'D2'])
    expect(result).toHaveLength(0)
  })

  it('mixed: only shows selected models that have been generated', () => {
    const result = resolveModelsFromGenerated(allModels, ['D1', 'D99', 'D3'])
    expect(result).toHaveLength(2)
    expect(result[0].id).toBe('sess-1/D1')
    expect(result[1].id).toBe('sess-1/D3')
  })
})
