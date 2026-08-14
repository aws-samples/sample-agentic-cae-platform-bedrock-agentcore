import type { ServerMessage } from '../../ws/types'

/**
 * Pure chat state model + reducer for the Chat panel (task 12.5).
 *
 * All server-driven state transitions flow through {@link applyServerMessage},
 * a pure function so token accumulation, result finalization, progress, and
 * error handling can be unit- and property-tested without React or a socket.
 *
 * Requirements: 4.1 (source references in answers), 5.2 (optimization results),
 * 12.1 (token-by-token streaming), 12.2 (live optimization progress).
 */

/** A source drawing reference surfaced beneath an answer (Req 4.1). */
export interface SourceRef {
  /** Drawing identifier the answer was grounded in. */
  drawingId: string
  /** Optional section/region reference within the drawing. */
  section?: string
}

/** A single chat bubble. */
export interface ChatMessage {
  /** Stable, unique key for rendering. */
  id: string
  role: 'user' | 'assistant'
  /** Rendered text (accumulates while `streaming` is true). */
  text: string
  /** True while assistant tokens are still arriving for this message. */
  streaming: boolean
  /** Source drawing references attached on finalization (Req 4.1). */
  sources?: SourceRef[]
}

/** Live optimization progress rendered during a run (Req 12.2). */
export interface OptimizationProgress {
  /** Current NSGA-II generation. */
  generation?: number
  /** Current hypervolume indicator. */
  hypervolume?: number
  /** Estimated time remaining, in seconds. */
  etaSeconds?: number
}

/** Immutable snapshot of the chat panel's server-driven state. */
export interface ChatState {
  messages: ChatMessage[]
  /** Non-null while an optimization run is in progress (Req 5.2 / 12.2). */
  progress: OptimizationProgress | null
  /** True between the first `progress` frame and the terminal `result`/`error`. */
  optimizing: boolean
  /** Most recent error message, or null. Cleared when new tokens arrive. */
  error: string | null
  /** Highest sequence number applied so far (enforces in-order delivery). */
  lastSequence: number
}

/** The starting state before any message is exchanged. */
export const initialChatState: ChatState = {
  messages: [],
  progress: null,
  optimizing: false,
  error: null,
  lastSequence: -1,
}

/** Read a string field from an unknown payload without throwing. */
function readString(payload: unknown, ...keys: string[]): string | undefined {
  if (typeof payload === 'string') {
    return payload
  }
  if (payload !== null && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    for (const key of keys) {
      const value = record[key]
      if (typeof value === 'string') {
        return value
      }
    }
  }
  return undefined
}

/** Read a finite number field from an unknown payload without throwing. */
function readNumber(payload: unknown, ...keys: string[]): number | undefined {
  if (payload !== null && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    for (const key of keys) {
      const value = record[key]
      if (typeof value === 'number' && Number.isFinite(value)) {
        return value
      }
    }
  }
  return undefined
}

/** Extract the text chunk carried by a `token` frame. */
export function extractTokenText(payload: unknown): string {
  return readString(payload, 'text', 'token', 'content') ?? ''
}

/** Extract optimization progress fields from a `progress` frame. */
export function extractProgress(payload: unknown): OptimizationProgress {
  return {
    generation: readNumber(payload, 'generation', 'current_generation', 'gen'),
    hypervolume: readNumber(payload, 'hypervolume', 'hv'),
    etaSeconds: readNumber(
      payload,
      'eta',
      'eta_seconds',
      'estimated_time_remaining',
      'etaSeconds',
    ),
  }
}

/** Extract the final answer text from a `result` frame, if present. */
export function extractAnswer(payload: unknown): string | undefined {
  return readString(payload, 'answer', 'text', 'summary', 'message')
}

/** Extract source drawing references from a `result` frame (Req 4.1). */
export function extractSources(payload: unknown): SourceRef[] {
  if (payload === null || typeof payload !== 'object') {
    return []
  }
  const raw = (payload as Record<string, unknown>).sources
  if (!Array.isArray(raw)) {
    return []
  }
  const sources: SourceRef[] = []
  for (const item of raw) {
    if (item === null || typeof item !== 'object') {
      continue
    }
    const drawingId = readString(item, 'drawing_id', 'drawingId', 'id')
    if (drawingId === undefined) {
      continue
    }
    const section = readString(item, 'section', 'section_ref', 'sectionRef')
    sources.push(section === undefined ? { drawingId } : { drawingId, section })
  }
  return sources
}

/** Extract the human-readable error message from an `error` frame. */
export function extractError(payload: unknown): string {
  return (
    readString(payload, 'message', 'error', 'detail') ??
    'Something went wrong. Please try again.'
  )
}

/** True when the last message is an assistant bubble still streaming tokens. */
function lastStreamingIndex(messages: ChatMessage[]): number {
  const index = messages.length - 1
  if (index < 0) {
    return -1
  }
  const last = messages[index]
  return last.role === 'assistant' && last.streaming ? index : -1
}

/** Replace one message immutably. */
function replaceAt(
  messages: ChatMessage[],
  index: number,
  next: ChatMessage,
): ChatMessage[] {
  const copy = messages.slice()
  copy[index] = next
  return copy
}

/**
 * Apply a server message to the chat state, returning a new state.
 *
 * Pure and deterministic: assistant message ids derive from the frame
 * `sequence`. Out-of-order or duplicate frames (`sequence <= lastSequence`)
 * are ignored to preserve in-order delivery (Req 12.7).
 */
export function applyServerMessage(
  state: ChatState,
  message: ServerMessage,
): ChatState {
  if (message.sequence <= state.lastSequence) {
    return state
  }
  const base: ChatState = { ...state, lastSequence: message.sequence }

  switch (message.type) {
    case 'token': {
      const chunk = extractTokenText(message.payload)
      const index = lastStreamingIndex(base.messages)
      if (index === -1) {
        const created: ChatMessage = {
          id: `a-${message.sequence}`,
          role: 'assistant',
          text: chunk,
          streaming: true,
        }
        return {
          ...base,
          error: null,
          messages: [...base.messages, created],
        }
      }
      const current = base.messages[index]
      return {
        ...base,
        error: null,
        messages: replaceAt(base.messages, index, {
          ...current,
          text: current.text + chunk,
        }),
      }
    }

    case 'progress': {
      return {
        ...base,
        optimizing: true,
        progress: extractProgress(message.payload),
      }
    }

    case 'stage': {
      // Stage notifications are informational; they advance the sequence only.
      return base
    }

    case 'result': {
      const answer = extractAnswer(message.payload)
      const sources = extractSources(message.payload)
      const index = lastStreamingIndex(base.messages)
      const finalized: ChatState = {
        ...base,
        optimizing: false,
        progress: null,
      }
      if (index === -1) {
        const created: ChatMessage = {
          id: `a-${message.sequence}`,
          role: 'assistant',
          text: answer ?? '',
          streaming: false,
          ...(sources.length > 0 ? { sources } : {}),
        }
        return { ...finalized, messages: [...base.messages, created] }
      }
      const current = base.messages[index]
      const text = current.text.length > 0 ? current.text : (answer ?? '')
      return {
        ...finalized,
        messages: replaceAt(base.messages, index, {
          ...current,
          text,
          streaming: false,
          ...(sources.length > 0 ? { sources } : {}),
        }),
      }
    }

    case 'error': {
      const index = lastStreamingIndex(base.messages)
      const messages =
        index === -1
          ? base.messages
          : replaceAt(base.messages, index, {
              ...base.messages[index],
              streaming: false,
            })
      return {
        ...base,
        optimizing: false,
        progress: null,
        error: extractError(message.payload),
        messages,
      }
    }

    default:
      return base
  }
}

/**
 * Convert persisted session messages (role/text records restored from the
 * sessions API) into chat bubbles for history rehydration (Req 13.3). Unknown
 * roles default to `assistant` only when explicitly "assistant", else `user`.
 */
export function messagesToChat(persisted: readonly unknown[]): ChatMessage[] {
  const out: ChatMessage[] = []
  let index = 0
  for (const raw of persisted) {
    if (raw === null || typeof raw !== 'object') {
      continue
    }
    const record = raw as Record<string, unknown>
    const text = typeof record.text === 'string' ? record.text : ''
    if (text.length === 0) {
      continue
    }
    const role = record.role === 'assistant' ? 'assistant' : 'user'
    out.push({ id: `h-${index}`, role, text, streaming: false })
    index += 1
  }
  return out
}

/** Build a fresh chat state seeded with restored history (Req 13.3). */
export function hydrateChatState(persisted: readonly unknown[]): ChatState {
  return { ...initialChatState, messages: messagesToChat(persisted) }
}

/** Append a user message immutably (used when the user submits input). */
export function appendUserMessage(
  state: ChatState,
  id: string,
  text: string,
): ChatState {
  const created: ChatMessage = {
    id,
    role: 'user',
    text,
    streaming: false,
  }
  return { ...state, error: null, messages: [...state.messages, created] }
}
