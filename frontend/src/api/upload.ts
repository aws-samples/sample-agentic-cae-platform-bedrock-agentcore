import { getAccessToken, notifyAuthExpired } from '../auth/tokens'

/**
 * HTTP upload client for the Drawing Ingestion Service (Req 1.1, 8.3).
 *
 * File uploads use the HTTP API (not the WebSocket gateway, per Req 8.3) via a
 * three-step presigned-URL flow:
 *   1. {@link requestPresignedUrl} — POST metadata, receive a `drawing_id` and
 *      a short-lived S3 presigned PUT URL.
 *   2. {@link putFile} — PUT the raw bytes directly to S3 (no auth header; the
 *      presigned URL carries its own signature).
 *   3. {@link confirmUpload} — POST the `drawing_id` so the backend writes
 *      metadata to DynamoDB and triggers extraction.
 *
 * All calls to the platform HTTP API attach the Cognito access token as a
 * bearer header and call {@link notifyAuthExpired} on a 401 so the auth layer
 * can sign the user out / redirect to login (Req 9.3), keeping redirect policy
 * in one place.
 */

/** Minimum accepted upload size in bytes (1 KB) — mirrors backend (Req 1.2). */
export const MIN_UPLOAD_SIZE_BYTES = 1024
/** Maximum accepted upload size in bytes (100 MB) — mirrors backend (Req 1.2). */
export const MAX_UPLOAD_SIZE_BYTES = 100 * 1024 * 1024
/** Accepted MIME type for drawings (Req 1.3). */
export const ACCEPTED_CONTENT_TYPE = 'application/pdf'
/** Accepted filename extension for drawings (Req 1.3). */
export const ACCEPTED_EXTENSION = '.pdf'

/**
 * Minimal structural view of a file for validation. The browser `File` type
 * satisfies this shape, but keeping it structural makes {@link validateUploadFile}
 * a pure, DOM-free function that is trivial to unit/property test.
 */
export interface UploadFileLike {
  name: string
  type: string
  size: number
}

/**
 * Validate a candidate upload against the backend rules (Req 1.2, 1.3) so we
 * can show inline errors before requesting a presigned URL. The backend
 * remains the source of truth and re-enforces the same rules.
 *
 * @returns an array of human-readable error messages; empty when the file is
 *   acceptable.
 */
export function validateUploadFile(file: UploadFileLike): string[] {
  const errors: string[] = []

  if (file.size < MIN_UPLOAD_SIZE_BYTES) {
    errors.push('File is too small. Accepted size is 1 KB to 100 MB.')
  } else if (file.size > MAX_UPLOAD_SIZE_BYTES) {
    errors.push('File is too large. Accepted size is 1 KB to 100 MB.')
  }

  if (!file.name.toLowerCase().endsWith(ACCEPTED_EXTENSION)) {
    errors.push('Only PDF files are accepted (a .pdf extension is required).')
  }

  if (file.type !== ACCEPTED_CONTENT_TYPE) {
    errors.push('Only PDF files are accepted (application/pdf content type is required).')
  }

  return errors
}

/** Metadata sent when requesting a presigned upload URL. */
export interface UploadMeta {
  filename: string
  contentType: string
  size: number
}

/** Result of {@link requestPresignedUrl}. */
export interface PresignedUrlResult {
  /** Server-generated unique drawing identifier (Req 1.1). */
  drawingId: string
  /** Short-lived S3 presigned PUT URL for the raw bytes. */
  uploadUrl: string
}

/**
 * Resolve the platform HTTP API base URL from Vite env.
 *
 * `VITE_API_ENDPOINT` is a CDK stack output and is intentionally not
 * hardcoded. When absent (e.g. local builds without a deployed backend) this
 * returns an empty string so relative requests are attempted; the app still
 * builds and renders.
 */
export function getApiEndpoint(): string {
  return import.meta.env.VITE_API_ENDPOINT ?? ''
}

/** Join the API base URL with a path, tolerating a trailing slash on the base. */
function apiUrl(path: string): string {
  const base = getApiEndpoint().replace(/\/+$/, '')
  return `${base}${path}`
}

/** Build an `Authorization` header, signalling auth expiry when no token exists. */
async function authHeaders(): Promise<Record<string, string>> {
  const token = await getAccessToken()
  if (token === null) {
    notifyAuthExpired()
    throw new Error('Not authenticated. Please sign in again.')
  }
  return { Authorization: `Bearer ${token}` }
}

/** Throw on a non-OK response, routing 401s through the auth-expiry handler. */
async function assertOk(response: Response, action: string): Promise<void> {
  if (response.ok) {
    return
  }
  if (response.status === 401) {
    notifyAuthExpired()
    throw new Error('Your session has expired. Please sign in again.')
  }
  let detail = ''
  try {
    detail = (await response.text()).slice(0, 500)
  } catch {
    // Ignore body read failures; the status code is enough context.
  }
  throw new Error(`${action} failed (HTTP ${response.status}).${detail ? ` ${detail}` : ''}`)
}

/**
 * Step 1: request a presigned S3 PUT URL and a `drawing_id` for the file.
 *
 * Accepts both the camelCase (`uploadUrl`) and snake_case (`upload_url`)
 * response shapes so it stays compatible with either backend serialization.
 */
export async function requestPresignedUrl(meta: UploadMeta): Promise<PresignedUrlResult> {
  const response = await fetch(apiUrl('/api/upload'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
    },
    body: JSON.stringify({
      filename: meta.filename,
      contentType: meta.contentType,
      content_type: meta.contentType,
      size: meta.size,
    }),
  })
  await assertOk(response, 'Requesting upload URL')

  const data = (await response.json()) as {
    drawing_id?: string
    drawingId?: string
    uploadUrl?: string
    upload_url?: string
  }
  const drawingId = data.drawingId ?? data.drawing_id
  const uploadUrl = data.uploadUrl ?? data.upload_url
  if (!drawingId || !uploadUrl) {
    throw new Error('Upload URL response was missing a drawing_id or upload URL.')
  }
  return { drawingId, uploadUrl }
}

/**
 * Step 2: PUT the raw file bytes directly to S3 via the presigned URL.
 *
 * No `Authorization` header is sent — the presigned URL is already signed. The
 * `Content-Type` must match what the presigned URL was generated for.
 */
export async function putFile(uploadUrl: string, file: Blob): Promise<void> {
  const response = await fetch(uploadUrl, {
    method: 'PUT',
    headers: { 'Content-Type': file.type || ACCEPTED_CONTENT_TYPE },
    body: file,
  })
  if (!response.ok) {
    throw new Error(`Uploading file to storage failed (HTTP ${response.status}).`)
  }
}

/**
 * Step 3: confirm the upload so the backend records metadata and triggers the
 * extraction pipeline (Req 1.5).
 */
export async function confirmUpload(drawingId: string, filename?: string): Promise<void> {
  const response = await fetch(apiUrl('/api/upload/confirm'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
    },
    body: JSON.stringify({
      drawing_id: drawingId,
      drawingId,
      ...(filename ? { filename } : {}),
    }),
  })
  await assertOk(response, 'Confirming upload')
}


/**
 * Delete a drawing by its id. Removes the PDF from S3, the Ground_Truth_JSON,
 * and the DynamoDB metadata row. Tolerates 404 (already gone).
 */
export async function deleteDrawing(drawingId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/upload/${encodeURIComponent(drawingId)}`), {
    method: 'DELETE',
    headers: { ...(await authHeaders()) },
  })
  if (response.status === 404 || response.ok) {
    return
  }
  await assertOk(response, 'Deleting drawing')
}
