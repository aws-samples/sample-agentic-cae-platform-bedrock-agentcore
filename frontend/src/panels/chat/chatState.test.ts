import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import type { ServerMessage } from '../../ws/types'
import {
  appendUserMessage,
  applyServerMessage,
  initialChatState,
} from './chatState'

/** Build a ServerMessage with sensible defaults for tests. */
function frame(
  type: ServerMessage['type'],
  payload: unknown,
  sequence: number,
): ServerMessage {
  return {
    type,
    payload,
    sequence,
    session_id: 'session-test',
    timestamp: new Date(0).toISOString(),
  }
}

describe('applyServerMessage', () => {
  it('creates a streaming assistant message from the first token', () => {
    const next = applyServerMessage(
      initialChatState,
      frame('token', { text: 'Hel' }, 0),
    )
    expect(next.messages).toHaveLength(1)
    expect(next.messages[0]).toMatchObject({
      role: 'assistant',
      text: 'Hel',
      streaming: true,
    })
    expect(next.lastSequence).toBe(0)
  })

  it('accumulates consecutive tokens into one assistant message', () => {
    let state = initialChatState
    state = applyServerMessage(state, frame('token', { text: 'Hel' }, 0))
    state = applyServerMessage(state, frame('token', { text: 'lo ' }, 1))
    state = applyServerMessage(state, frame('token', { text: 'world' }, 2))
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].text).toBe('Hello world')
    expect(state.messages[0].streaming).toBe(true)
  })

  it('accepts a bare string token payload', () => {
    const next = applyServerMessage(initialChatState, frame('token', 'hi', 0))
    expect(next.messages[0].text).toBe('hi')
  })

  it('finalizes the streaming message on result and attaches sources', () => {
    let state = applyServerMessage(
      initialChatState,
      frame('token', { text: 'The bolt is M8.' }, 0),
    )
    state = applyServerMessage(
      state,
      frame(
        'result',
        {
          sources: [
            { drawing_id: 'DRW-001', section: 'Title Block' },
            { drawing_id: 'DRW-002' },
          ],
        },
        1,
      ),
    )
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].streaming).toBe(false)
    expect(state.messages[0].text).toBe('The bolt is M8.')
    expect(state.messages[0].sources).toEqual([
      { drawingId: 'DRW-001', section: 'Title Block' },
      { drawingId: 'DRW-002' },
    ])
  })

  it('creates a finalized assistant message when result arrives without tokens', () => {
    const next = applyServerMessage(
      initialChatState,
      frame('result', { answer: 'Full answer.' }, 0),
    )
    expect(next.messages).toHaveLength(1)
    expect(next.messages[0]).toMatchObject({
      role: 'assistant',
      text: 'Full answer.',
      streaming: false,
    })
  })

  it('tracks optimization progress from progress frames', () => {
    const next = applyServerMessage(
      initialChatState,
      frame('progress', { generation: 12, hypervolume: 0.87, eta: 45 }, 0),
    )
    expect(next.optimizing).toBe(true)
    expect(next.progress).toEqual({
      generation: 12,
      hypervolume: 0.87,
      etaSeconds: 45,
    })
  })

  it('clears progress and optimizing on result', () => {
    let state = applyServerMessage(
      initialChatState,
      frame('progress', { generation: 3, hypervolume: 0.5, eta: 10 }, 0),
    )
    state = applyServerMessage(state, frame('result', { answer: 'Done' }, 1))
    expect(state.optimizing).toBe(false)
    expect(state.progress).toBeNull()
  })

  it('records an error and stops streaming on error frames', () => {
    let state = applyServerMessage(
      initialChatState,
      frame('token', { text: 'partial' }, 0),
    )
    state = applyServerMessage(
      state,
      frame('error', { message: 'QA timed out' }, 1),
    )
    expect(state.error).toBe('QA timed out')
    expect(state.messages[0].streaming).toBe(false)
    expect(state.optimizing).toBe(false)
  })

  it('provides a fallback error message when payload has none', () => {
    const next = applyServerMessage(initialChatState, frame('error', {}, 0))
    expect(next.error).toBeTruthy()
  })

  it('ignores out-of-order and duplicate frames', () => {
    let state = applyServerMessage(
      initialChatState,
      frame('token', { text: 'a' }, 5),
    )
    const stale = applyServerMessage(state, frame('token', { text: 'b' }, 5))
    expect(stale).toBe(state)
    const older = applyServerMessage(state, frame('token', { text: 'c' }, 2))
    expect(older).toBe(state)
    state = applyServerMessage(state, frame('token', { text: 'd' }, 6))
    expect(state.messages[0].text).toBe('ad')
  })

  it('clears a prior error when a new token stream begins', () => {
    let state = applyServerMessage(
      initialChatState,
      frame('error', { message: 'boom' }, 0),
    )
    state = applyServerMessage(state, frame('token', { text: 'retry' }, 1))
    expect(state.error).toBeNull()
  })

  // Validates: Requirements 12.1, 12.7
  it('property: in-order token frames yield the concatenation in sequence order', () => {
    fc.assert(
      fc.property(fc.array(fc.string(), { minLength: 1 }), (chunks) => {
        let state = initialChatState
        chunks.forEach((chunk, i) => {
          state = applyServerMessage(state, frame('token', { text: chunk }, i))
        })
        expect(state.messages).toHaveLength(1)
        expect(state.messages[0].text).toBe(chunks.join(''))
        expect(state.messages[0].streaming).toBe(true)
        expect(state.lastSequence).toBe(chunks.length - 1)
      }),
    )
  })
})

describe('appendUserMessage', () => {
  it('appends a user bubble and clears any error', () => {
    let state = applyServerMessage(
      initialChatState,
      frame('error', { message: 'boom' }, 0),
    )
    state = appendUserMessage(state, 'u-0', 'How wide is the flange?')
    const user = state.messages.at(-1)
    expect(user).toMatchObject({
      id: 'u-0',
      role: 'user',
      text: 'How wide is the flange?',
      streaming: false,
    })
    expect(state.error).toBeNull()
  })
})
