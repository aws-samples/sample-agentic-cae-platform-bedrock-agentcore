import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import { cameraFitDistance, capModelSelection, normalizedClipConstant } from './geometry'
import { MAX_MODELS } from './types'

describe('capModelSelection (Req 6.5)', () => {
  it('returns the whole list when under the cap', () => {
    expect(capModelSelection([1, 2, 3])).toEqual([1, 2, 3])
  })

  it('truncates to at most 4 models by default', () => {
    expect(capModelSelection([1, 2, 3, 4, 5, 6])).toEqual([1, 2, 3, 4])
  })

  it('respects a custom cap', () => {
    expect(capModelSelection(['a', 'b', 'c'], 2)).toEqual(['a', 'b'])
  })

  it('returns empty for a non-positive cap', () => {
    expect(capModelSelection([1, 2, 3], 0)).toEqual([])
    expect(capModelSelection([1, 2, 3], -1)).toEqual([])
  })

  it('does not mutate the input', () => {
    const input = [1, 2, 3, 4, 5]
    capModelSelection(input)
    expect(input).toEqual([1, 2, 3, 4, 5])
  })

  it('property: result length never exceeds the cap and is an order-preserving prefix', () => {
    fc.assert(
      fc.property(
        fc.array(fc.integer()),
        fc.integer({ min: 0, max: 10 }),
        (list, max) => {
          const result = capModelSelection(list, max)
          expect(result.length).toBeLessThanOrEqual(max)
          expect(result.length).toBeLessThanOrEqual(list.length)
          // Order-preserving prefix of the original list.
          expect(result).toEqual(list.slice(0, result.length))
        },
      ),
    )
  })

  it('property: default cap keeps at most MAX_MODELS', () => {
    fc.assert(
      fc.property(fc.array(fc.integer()), (list) => {
        expect(capModelSelection(list).length).toBeLessThanOrEqual(MAX_MODELS)
      }),
    )
  })
})

describe('cameraFitDistance (Req 6.2)', () => {
  it('is zero for a zero-radius model', () => {
    expect(cameraFitDistance(0, 50)).toBe(0)
  })

  it('produces a finite positive distance for a real model', () => {
    const d = cameraFitDistance(10, 50)
    expect(d).toBeGreaterThan(0)
    expect(Number.isFinite(d)).toBe(true)
  })

  it('property: strictly increasing in bounding radius for a fixed fov', () => {
    fc.assert(
      fc.property(
        fc.double({ min: 0.1, max: 1e4, noNaN: true }),
        fc.double({ min: 0.1, max: 1e4, noNaN: true }),
        fc.double({ min: 5, max: 175, noNaN: true }),
        (r1, r2, fov) => {
          // Require a meaningful relative gap between the radii. Two radii that
          // differ only by a floating-point ULP can map to identical doubles
          // (the distance is linear in the radius), which would fail a strict
          // `>` without any real loss of monotonicity.
          fc.pre(r2 - r1 > Math.max(1, r2) * 1e-9)
          expect(cameraFitDistance(r2, fov)).toBeGreaterThan(cameraFitDistance(r1, fov))
        },
      ),
    )
  })
})

describe('normalizedClipConstant (Req 6.2)', () => {
  it('maps the midpoint to the model centre', () => {
    expect(normalizedClipConstant(0.5, 20)).toBeCloseTo(0)
  })

  it('maps the extremes to +/- radius', () => {
    expect(normalizedClipConstant(0, 20)).toBeCloseTo(-20)
    expect(normalizedClipConstant(1, 20)).toBeCloseTo(20)
  })

  it('property: result stays within [-radius, radius]', () => {
    fc.assert(
      fc.property(
        fc.double({ min: -5, max: 5, noNaN: true }),
        fc.double({ min: 0, max: 1e4, noNaN: true }),
        (offset, radius) => {
          const c = normalizedClipConstant(offset, radius)
          expect(c).toBeGreaterThanOrEqual(-radius - 1e-9)
          expect(c).toBeLessThanOrEqual(radius + 1e-9)
        },
      ),
    )
  })
})
