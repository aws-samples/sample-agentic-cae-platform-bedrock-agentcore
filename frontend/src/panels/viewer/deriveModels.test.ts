import { describe, expect, it } from 'vitest'
import { deriveModelsFromPayload } from './deriveModels'

describe('deriveModelsFromPayload (Req 6.5, defensive WS read)', () => {
  it('returns empty for non-object payloads', () => {
    expect(deriveModelsFromPayload(null)).toEqual([])
    expect(deriveModelsFromPayload('nope')).toEqual([])
    expect(deriveModelsFromPayload(42)).toEqual([])
    expect(deriveModelsFromPayload({})).toEqual([])
  })

  it('parses a single model_url / stl_url / url', () => {
    expect(deriveModelsFromPayload({ model_url: 's3://b/a.stl' })).toEqual([
      { id: 'model-0', stlUrl: 's3://b/a.stl', label: undefined },
    ])
    expect(deriveModelsFromPayload({ stl_url: 's3://b/c.stl' })[0].stlUrl).toBe(
      's3://b/c.stl',
    )
    expect(deriveModelsFromPayload({ url: 's3://b/d.stl' })[0].stlUrl).toBe('s3://b/d.stl')
  })

  it('parses an array of model descriptors, ignoring malformed entries', () => {
    const result = deriveModelsFromPayload({
      models: [
        { id: 'x', stl_url: 's3://b/1.stl', label: 'One' },
        { url: 's3://b/2.stl' },
        { nope: true },
      ],
    })
    expect(result).toEqual([
      { id: 'x', stlUrl: 's3://b/1.stl', label: 'One' },
      { id: 'model-1', stlUrl: 's3://b/2.stl', label: undefined },
    ])
  })

  it('parses an array of bare URLs', () => {
    expect(deriveModelsFromPayload({ model_urls: ['s3://b/1.stl', 's3://b/2.stl'] })).toEqual(
      [
        { id: 'model-0', stlUrl: 's3://b/1.stl' },
        { id: 'model-1', stlUrl: 's3://b/2.stl' },
      ],
    )
  })
})
