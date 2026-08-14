import { describe, expect, it, vi } from 'vitest'
import {
  requestModelGeneration,
  type Generate3DModelRequest,
} from './generate3DModel'
import type { WebSocketLike } from '../ws/types'

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

describe('requestModelGeneration (Req 3.1, 3.5)', () => {
  it('sends a generate_model action with correct payload shape', () => {
    const ws = createFakeSocket()
    const request: Generate3DModelRequest = {
      sessionId: 'session-abc',
      designId: 'design-1',
      drawingId: 'drawing-abc',
      objectives: { weight_kg: 1.2, heat_dissipation_W: 45.0 },
      parameters: { fin_count: 12, base_thickness_mm: 3.5 },
    }

    requestModelGeneration(ws, request)

    expect(ws.sent).toHaveLength(1)
    const parsed = JSON.parse(ws.sent[0])
    expect(parsed).toEqual({
      action: 'generate_model',
      session_id: 'session-abc',
      data: {
        session_id: 'session-abc',
        design_id: 'design-1',
        drawing_id: 'drawing-abc',
        objectives: { weight_kg: 1.2, heat_dissipation_W: 45.0 },
        parameters: { fin_count: 12, base_thickness_mm: 3.5 },
      },
    })
  })

  it('sends data.drawing_id equal to the request drawingId (Req 3.1)', () => {
    const ws = createFakeSocket()
    const request: Generate3DModelRequest = {
      sessionId: 'session-draw',
      designId: 'design-draw',
      drawingId: 'drawing-xyz-123',
      objectives: { weight_kg: 0.8 },
    }

    requestModelGeneration(ws, request)

    const parsed = JSON.parse(ws.sent[0])
    expect(parsed.data.drawing_id).toBe(request.drawingId)
    expect(parsed.data.drawing_id).toBe('drawing-xyz-123')
  })

  it('defaults parameters to empty object when omitted', () => {
    const ws = createFakeSocket()
    const request: Generate3DModelRequest = {
      sessionId: 'session-xyz',
      designId: 'design-2',
      drawingId: 'drawing-xyz',
      objectives: { mass: 0.5 },
    }

    requestModelGeneration(ws, request)

    const parsed = JSON.parse(ws.sent[0])
    expect(parsed.data.parameters).toEqual({})
  })

  it('includes session_id at the top level for routing', () => {
    const ws = createFakeSocket()
    const request: Generate3DModelRequest = {
      sessionId: 'routing-session',
      designId: 'design-3',
      drawingId: 'drawing-routing',
      objectives: { cost: 100 },
    }

    requestModelGeneration(ws, request)

    const parsed = JSON.parse(ws.sent[0])
    expect(parsed.session_id).toBe('routing-session')
  })

  it('preserves all objective key-value pairs in the payload', () => {
    const ws = createFakeSocket()
    const objectives = {
      weight_kg: 2.5,
      max_stress_mpa: 150.0,
      cost_usd: 42.99,
      thermal_resistance: 0.03,
    }
    const request: Generate3DModelRequest = {
      sessionId: 'sess-1',
      designId: 'design-multi',
      drawingId: 'drawing-multi',
      objectives,
    }

    requestModelGeneration(ws, request)

    const parsed = JSON.parse(ws.sent[0])
    expect(parsed.data.objectives).toEqual(objectives)
  })

  it('sends valid JSON string via ws.send', () => {
    const ws = createFakeSocket()
    const request: Generate3DModelRequest = {
      sessionId: 's1',
      designId: 'd1',
      drawingId: 'dr1',
      objectives: { x: 1 },
    }

    requestModelGeneration(ws, request)

    // Should not throw on parse
    expect(() => JSON.parse(ws.sent[0])).not.toThrow()
  })
})
