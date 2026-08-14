import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getModelGenerationStates, normalizeSummary } from './sessions'

// getModelGenerationStates resolves the API base URL via getApiEndpoint and the
// bearer token via getAccessToken; stub both so the function under test only
// depends on the mocked fetch response.
vi.mock('./upload', () => ({
  getApiEndpoint: () => 'https://api.example.com',
}))
vi.mock('../auth/tokens', () => ({
  getAccessToken: async () => 'test-token',
  notifyAuthExpired: () => {},
}))

/** Build a minimal ok Response whose json() resolves to `body`. */
function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response
}

describe('normalizeSummary (Req 13.6)', () => {
  it('normalizes a well-formed camelCase record', () => {
    expect(
      normalizeSummary({
        sessionId: 's-1',
        title: 'Bracket review',
        lastAccessAt: '2024-03-01T00:00:00.000Z',
        messageCount: 7,
      }),
    ).toEqual({
      sessionId: 's-1',
      title: 'Bracket review',
      lastAccessAt: '2024-03-01T00:00:00.000Z',
      messageCount: 7,
    })
  })

  it('accepts snake_case keys and a summary fallback for title', () => {
    expect(
      normalizeSummary({
        session_id: 's-2',
        summary: 'Gearbox chat',
        last_access_at: '2024-02-01T00:00:00.000Z',
        message_count: 3,
      }),
    ).toEqual({
      sessionId: 's-2',
      title: 'Gearbox chat',
      lastAccessAt: '2024-02-01T00:00:00.000Z',
      messageCount: 3,
    })
  })

  it('defaults missing title and message count', () => {
    const result = normalizeSummary({ sessionId: 's-3' })
    expect(result).toEqual({
      sessionId: 's-3',
      title: 'Untitled session',
      lastAccessAt: '',
      messageCount: 0,
    })
  })

  it('returns null for records without a usable session id', () => {
    expect(normalizeSummary({ title: 'no id' })).toBeNull()
    expect(normalizeSummary(null)).toBeNull()
    expect(normalizeSummary('nope')).toBeNull()
    expect(normalizeSummary({ sessionId: '' })).toBeNull()
  })
})

describe('getModelGenerationStates (Req 2.2)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('returns the parsed modelStates array from the response', async () => {
    const modelStates = [
      { designId: 'd1', status: 'generating' },
      { designId: 'd2', status: 'ready', modelUrl: 'https://cdn.example.com/d2.stl' },
      { designId: 'd3', status: 'error', error: 'insufficient_geometry' },
    ]
    const fetchMock = vi.fn(async () => jsonResponse({ modelStates }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await getModelGenerationStates('drawing-123')

    expect(result).toEqual(modelStates)
    // Drawing id is passed through, URL-encoded, on the modelsForDrawing query param.
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.example.com/api/sessions?modelsForDrawing=drawing-123',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('returns an empty array when the modelStates field is absent', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({})))

    const result = await getModelGenerationStates('drawing-abc')

    expect(result).toEqual([])
  })

  it('returns an empty array when modelStates is not an array', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ modelStates: 'nope' })))

    const result = await getModelGenerationStates('drawing-xyz')

    expect(result).toEqual([])
  })
})
