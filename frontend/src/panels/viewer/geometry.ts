/**
 * Pure, framework-free geometry helpers for the 3D Viewer (Requirements 6.2, 6.5).
 *
 * These functions contain the only non-trivial math in the panel and are kept
 * side-effect free so they can be unit- and property-tested without a WebGL
 * context (design §6, testing guidance).
 */

import { MAX_MODELS } from './types'

/**
 * Enforce the "up to N models side-by-side" cap (Req 6.5). Returns a prefix of
 * `list` of length at most `max`, preserving order. A non-positive `max` yields
 * an empty array. Never mutates the input.
 */
export function capModelSelection<T>(list: readonly T[], max: number = MAX_MODELS): T[] {
  if (max <= 0) return []
  return list.slice(0, max)
}

/**
 * Camera distance (along the view axis) required to fit a bounding sphere of the
 * given `radius` within a perspective camera's vertical field of view (Req 6.2,
 * "initial view within 5s of model load" — a correct fit avoids extra framing
 * round-trips).
 *
 * Derived from `sin(fov/2) = radius / distance`, with a `margin` (>= 1) so the
 * model does not touch the viewport edges. Strictly increasing in `radius`,
 * which the property test relies on.
 *
 * @param radius Bounding-sphere radius (> 0).
 * @param fovDeg Vertical field of view in degrees, exclusive of 0 and 180.
 * @param margin Padding factor applied to the distance (>= 1). Defaults to 1.2.
 */
export function cameraFitDistance(radius: number, fovDeg: number, margin: number = 1.2): number {
  const safeRadius = Math.max(radius, 0)
  const clampedFov = Math.min(Math.max(fovDeg, 1), 179)
  const halfFovRad = (clampedFov * Math.PI) / 180 / 2
  const safeMargin = Math.max(margin, 1)
  return (safeRadius / Math.sin(halfFovRad)) * safeMargin
}

/**
 * Map a normalized cross-section slider value `offset` in [0, 1] to a clipping
 * plane constant in [-radius, +radius] (Req 6.2, cross-section control).
 *
 * `offset = 0` clips at one extreme of the model, `offset = 1` at the other, and
 * `offset = 0.5` passes through the model centre (constant 0). The result is
 * clamped to the model's extent so the plane never leaves the geometry.
 */
export function normalizedClipConstant(offset: number, radius: number): number {
  const safeRadius = Math.max(radius, 0)
  const clampedOffset = Math.min(Math.max(offset, 0), 1)
  return (clampedOffset * 2 - 1) * safeRadius
}
