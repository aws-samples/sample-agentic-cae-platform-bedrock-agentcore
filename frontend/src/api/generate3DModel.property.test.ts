import { describe, expect, it, vi } from 'vitest'
import fc from 'fast-check'
import {
  requestModelGeneration,
  type Generate3DModelRequest,
} from './generate3DModel'
import type { WebSocketLike } from '../ws/types'

/**
 * **Validates: Requirements 3.2, 3.5, 4.2**
 *
 * Property 4: Model generation request includes session ID, design ID, and
 * design parameters.
 *
 * For any Pareto design selection triggering 3D model generation, the request
 * payload sent to the generate_3d_model tool SHALL contain the session ID,
 * design ID, and the design's objective values.
 */

// ─── Test helpers ────────────────────────────────────────────────────────────

/** Minimal fake WebSocket that captures sent messages. */
function createFakeSocket(): WebSocketLike & { sent: string[] } {
  return {
    readyState: 1,
    sent: [] as string[],
    send(data: string) {
      this.sent.push(data)
    },
    close: vi.fn(),
    onopen: null,
    onmessage: null,
    onclose: null,
    onerror: null,
  }
}

// ─── Arbitraries ─────────────────────────────────────────────────────────────

/** Arbitrary for a non-empty alphanumeric-like string (simulates IDs). */
const arbId = fc.string({ minLength: 1, maxLength: 30 }).filter((s) => s.trim().length > 0)

/** Arbitrary for objective key-value maps (1–6 objectives with numeric values). */
const arbObjectives = fc.dictionary(
  fc.string({ minLength: 1, maxLength: 20 }).filter((s) => s.trim().length > 0),
  fc.double({ min: -10000, max: 10000, noNaN: true }).map((v) => (Object.is(v, -0) ? 0 : v)),
  { minKeys: 1, maxKeys: 6 },
)

/** Arbitrary for optional parameters map. */
const arbParameters = fc.option(
  fc.dictionary(
    fc.string({ minLength: 1, maxLength: 15 }).filter((s) => s.trim().length > 0),
    fc.oneof(
      fc.double({ min: -1000, max: 1000, noNaN: true }),
      fc.integer({ min: 0, max: 100 }),
      fc.string({ minLength: 1, maxLength: 10 }),
      fc.boolean(),
    ),
    { minKeys: 0, maxKeys: 5 },
  ),
  { nil: undefined },
)

/** Arbitrary for a full Generate3DModelRequest simulating a ParetoDesign selection. */
const arbGenerateRequest: fc.Arbitrary<Generate3DModelRequest> = fc.record({
  sessionId: arbId,
  designId: arbId,
  drawingId: arbId,
  objectives: arbObjectives,
  parameters: arbParameters,
})

// ─── Property Tests ──────────────────────────────────────────────────────────

describe('Property 4: Model generation request completeness (Req 3.2, 3.5, 4.2)', () => {
  it('property: request payload always includes sessionId in data.session_id', () => {
    fc.assert(
      fc.property(arbGenerateRequest, (request) => {
        const ws = createFakeSocket()
        requestModelGeneration(ws, request)

        expect(ws.sent).toHaveLength(1)
        const parsed = JSON.parse(ws.sent[0])

        // Session ID present at top level for WebSocket routing
        expect(parsed.session_id).toBe(request.sessionId)
        // Session ID present in data payload for the generation tool
        expect(parsed.data.session_id).toBe(request.sessionId)
      }),
    )
  })

  it('property: request payload always includes designId in data.design_id', () => {
    fc.assert(
      fc.property(arbGenerateRequest, (request) => {
        const ws = createFakeSocket()
        requestModelGeneration(ws, request)

        const parsed = JSON.parse(ws.sent[0])

        // Design ID must always be present in the data payload
        expect(parsed.data.design_id).toBe(request.designId)
      }),
    )
  })

  it('property: request payload always includes all objectives from the design', () => {
    fc.assert(
      fc.property(arbGenerateRequest, (request) => {
        const ws = createFakeSocket()
        requestModelGeneration(ws, request)

        const parsed = JSON.parse(ws.sent[0])

        // All objectives from the selected design must appear in the payload
        expect(parsed.data.objectives).toEqual(request.objectives)
        // Verify non-empty (ParetoDesign always has at least one objective)
        expect(Object.keys(parsed.data.objectives).length).toBeGreaterThan(0)
      }),
    )
  })

  it('property: request uses "generate_model" action for all designs', () => {
    fc.assert(
      fc.property(arbGenerateRequest, (request) => {
        const ws = createFakeSocket()
        requestModelGeneration(ws, request)

        const parsed = JSON.parse(ws.sent[0])

        // Action must always be "generate_model" regardless of design content
        expect(parsed.action).toBe('generate_model')
      }),
    )
  })

  it('property: request payload always includes parameters (defaults to empty object)', () => {
    fc.assert(
      fc.property(arbGenerateRequest, (request) => {
        const ws = createFakeSocket()
        requestModelGeneration(ws, request)

        const parsed = JSON.parse(ws.sent[0])

        // Parameters must always be present (empty object if not provided)
        expect(parsed.data.parameters).toBeDefined()
        expect(typeof parsed.data.parameters).toBe('object')

        // When parameters provided, they should match; otherwise default to {}
        if (request.parameters !== undefined) {
          expect(parsed.data.parameters).toEqual(request.parameters)
        } else {
          expect(parsed.data.parameters).toEqual({})
        }
      }),
    )
  })

  it('property: payload is always valid JSON containing all required fields', () => {
    fc.assert(
      fc.property(arbGenerateRequest, (request) => {
        const ws = createFakeSocket()
        requestModelGeneration(ws, request)

        // Sent message must be parseable JSON
        expect(() => JSON.parse(ws.sent[0])).not.toThrow()

        const parsed = JSON.parse(ws.sent[0])

        // Validate presence of all required top-level fields
        expect(parsed).toHaveProperty('action')
        expect(parsed).toHaveProperty('session_id')
        expect(parsed).toHaveProperty('data')

        // Validate presence of all required data fields
        expect(parsed.data).toHaveProperty('session_id')
        expect(parsed.data).toHaveProperty('design_id')
        expect(parsed.data).toHaveProperty('objectives')
        expect(parsed.data).toHaveProperty('parameters')
      }),
    )
  })
})
