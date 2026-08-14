/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** WebSocket endpoint (CDK stack output). Optional for local builds. */
  readonly VITE_WEBSOCKET_ENDPOINT?: string
  /** HTTP API base URL for uploads (CDK stack output). Optional for local builds. */
  readonly VITE_API_ENDPOINT?: string
  /** Cognito User Pool ID (CDK stack output). Optional for local builds. */
  readonly VITE_COGNITO_USER_POOL_ID?: string
  /** Cognito App Client ID (CDK stack output). Optional for local builds. */
  readonly VITE_COGNITO_APP_CLIENT_ID?: string
  /** AWS region of the Cognito User Pool. Optional; derived from pool id. */
  readonly VITE_AWS_REGION?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
