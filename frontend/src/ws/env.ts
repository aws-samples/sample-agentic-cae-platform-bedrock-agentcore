/**
 * Resolves the WebSocket endpoint from Vite env.
 *
 * `VITE_WEBSOCKET_ENDPOINT` is a CDK stack output and is intentionally not
 * hardcoded. When it is absent (e.g. local builds without a deployed backend)
 * this returns an empty string and the provider skips connecting, so the app
 * still builds and renders.
 */
export function getWebSocketEndpoint(): string {
  return import.meta.env.VITE_WEBSOCKET_ENDPOINT ?? ''
}
