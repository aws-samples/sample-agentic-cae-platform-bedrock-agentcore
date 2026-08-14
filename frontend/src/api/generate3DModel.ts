/**
 * API utility for triggering 3D model generation from Pareto design selection.
 *
 * When a user selects a design from the Results Dashboard, this module sends
 * a `generate_model` action via the WebSocket connection. The backend invokes
 * the generate_3d_model Lambda tool, produces an STL file in the Models S3
 * bucket, and responds with a CloudFront URL via a `result` frame containing
 * `action: "model_generated"`.
 *
 * Requirements: 3.1, 3.5
 */

import type { WebSocketLike } from '../ws/types'

/** Payload sent to the backend to trigger 3D model generation (Req 3.1, 3.5). */
export interface Generate3DModelRequest {
  /** Active session identifier for traceability. */
  sessionId: string
  /** Stable design identifier from the Pareto front. */
  designId: string
  /** Active drawing whose ground-truth drives geometry generation. */
  drawingId: string
  /** Objective name → numeric value map from the selected ParetoDesign. */
  objectives: Record<string, number>
  /** Optional design parameters (e.g. fin_count, base_thickness_mm). */
  parameters?: Record<string, unknown>
}

/** Shape of the server response delivered as a WebSocket `result` frame. */
export interface Generate3DModelResponse {
  /** CloudFront URL for the generated STL model. */
  modelUrl: string
  /** Session that owns this model. */
  sessionId: string
  /** Design that produced this model. */
  designId: string
}

/**
 * Send a `generate_model` action via the WebSocket connection to trigger
 * 3D STL generation for the selected Pareto design.
 *
 * The response arrives asynchronously as a server `result` frame with
 * `payload.action === "model_generated"` — callers should listen on the
 * WebSocket message stream for that frame.
 *
 * @param ws  - An open WebSocket (or WebSocketLike) connection.
 * @param request - The generation request payload.
 *
 * Requirements: 3.1, 3.5
 */
export function requestModelGeneration(
  ws: WebSocket | WebSocketLike,
  request: Generate3DModelRequest,
): void {
  const message = JSON.stringify({
    action: 'generate_model',
    session_id: request.sessionId,
    data: {
      session_id: request.sessionId,
      design_id: request.designId,
      drawing_id: request.drawingId,
      objectives: request.objectives,
      parameters: request.parameters ?? {},
    },
  })
  ws.send(message)
}
