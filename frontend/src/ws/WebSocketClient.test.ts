import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import { WebSocketClient } from './WebSocketClient'
import type {
  ConnectionStatus,
  ServerMessage,
  TimerHandle,
  WebSocketLike,
} from './types'

const ENDPOINT = 'wss://example.test/prod'
const SESSION_ID = 'session-123'

/** In-memory socket that the test drives via server* helpers. */
class FakeWebSocket implements WebSocketLike {
  readyState = 0
  readonly sent: string[] = []
  onopen: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose:
    | ((event: { code?: number; reason?: string; wasClean?: boolean }) => void)
    | null = null
  onerror: ((event: unknown) => void) | null = null
  readonly url: string

  constructor(url: string) {
    this.url = url
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(code?: number): void {
    this.readyState = 3
    this.onclose?.({ code })
  }

  // --- test drivers ---
  serverOpen(): void {
    this.readyState = 1
    this.onopen?.({})
  }

  serverMessage(message: ServerMessage): void {
    this.onmessage?.({ data: JSON.stringify(message) })
  }

  serverDrop(code = 1006): void {
    this.readyState = 3
    this.onclose?.({ code })
  }

  parsedSent(): Array<Record<string, unknown>> {
    return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>)
  }
}

/** Manual clock so reconnection timing is deterministic. */
class FakeClock {
  readonly delays: number[] = []
  private timers: Array<{ id: number; cb: () => void }> = []
  private seq = 0

  setTimeout = (cb: () => void, ms: number): TimerHandle => {
    this.delays.push(ms)
    const id = ++this.seq
    this.timers.push({ id, cb })
    return id
  }

  clearTimeout = (handle: TimerHandle): void => {
    this.timers = this.timers.filter((t) => t.id !== handle)
  }

  fireNext(): void {
    const timer = this.timers.shift()
    timer?.cb()
  }
}

/** Let pending microtasks (awaited token fetch inside connect) settle. */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function makeHarness() {
  const sockets: FakeWebSocket[] = []
  const clock = new FakeClock()
  const client = new WebSocketClient({
    endpoint: ENDPOINT,
    sessionId: SESSION_ID,
    getToken: async () => 'access-token',
    socketFactory: (url) => {
      const socket = new FakeWebSocket(url)
      sockets.push(socket)
      return socket
    },
    setTimeoutFn: clock.setTimeout,
    clearTimeoutFn: clock.clearTimeout,
  })
  const latest = () => sockets[sockets.length - 1]
  return { sockets, clock, client, latest }
}

function serverMessage(sequence: number): ServerMessage {
  return {
    type: 'token',
    session_id: SESSION_ID,
    payload: `chunk-${sequence}`,
    timestamp: new Date(0).toISOString(),
    sequence,
  }
}

describe('WebSocketClient — connection + auth', () => {
  it('builds the connect URL with token and session_id query params', async () => {
    const { client, latest } = makeHarness()
    await client.connect()
    expect(latest().url).toBe(
      `${ENDPOINT}?token=access-token&session_id=${SESSION_ID}`,
    )
  })
})

describe('WebSocketClient — reconnection (Req 12.4, 12.5)', () => {
  it('follows the exact backoff schedule then transitions to failed after 5 attempts', async () => {
    const { client, clock, latest } = makeHarness()
    const statuses: ConnectionStatus[] = []
    client.onStatusChange((s) => statuses.push(s))

    await client.connect()
    latest().serverOpen()
    expect(client.getStatus()).toBe('open')

    latest().serverDrop() // unexpected drop -> begin reconnecting

    for (let i = 0; i < 5; i += 1) {
      clock.fireNext()
      await flush()
      latest().serverDrop()
    }

    expect(clock.delays).toEqual([1000, 2000, 4000, 8000, 15000])
    expect(client.getStatus()).toBe('failed')
    expect(statuses).toContain('reconnecting')
    expect(statuses[statuses.length - 1]).toBe('failed')
  })

  it('manual reconnect() resets attempts and reconnects after failure', async () => {
    const { client, clock, latest } = makeHarness()
    await client.connect()
    latest().serverOpen()
    latest().serverDrop()
    for (let i = 0; i < 5; i += 1) {
      clock.fireNext()
      await flush()
      latest().serverDrop()
    }
    expect(client.getStatus()).toBe('failed')

    client.reconnect()
    await flush()
    latest().serverOpen()
    expect(client.getStatus()).toBe('open')
  })
})

describe('WebSocketClient — action queueing (Req 8.6)', () => {
  it('property: actions sent while disconnected are flushed in order on connect', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(
          fc.record({
            action: fc.constantFrom(
              'sendmessage' as const,
              'optimize' as const,
              'upload_complete' as const,
            ),
            data: fc.jsonValue(),
          }),
          { maxLength: 20 },
        ),
        async (actions) => {
          const { client, latest } = makeHarness()

          for (const a of actions) {
            client.send(a.action, a.data)
          }
          expect(client.getQueueLength()).toBe(actions.length)

          await client.connect()
          latest().serverOpen()

          const sent = latest().parsedSent()
          expect(sent).toHaveLength(actions.length)
          expect(
            sent.map((m) => ({ action: m.action, data: m.data })),
          ).toEqual(actions)
          expect(sent.every((m) => m.session_id === SESSION_ID)).toBe(true)
        },
      ),
    )
  })
})

describe('WebSocketClient — missed messages + ordering (Req 12.4)', () => {
  it('requests missed messages on reconnect with the correct last_sequence', async () => {
    const { client, clock, latest } = makeHarness()
    await client.connect()
    latest().serverOpen()

    latest().serverMessage(serverMessage(1))
    latest().serverMessage(serverMessage(2))
    latest().serverMessage(serverMessage(3))
    expect(client.getLastSequence()).toBe(3)

    latest().serverDrop()
    clock.fireNext()
    await flush()
    latest().serverOpen()

    const firstSent = latest().parsedSent()[0]
    expect(firstSent).toEqual({
      action: 'sendmessage',
      session_id: SESSION_ID,
      data: { type: 'resync', last_sequence: 3 },
    })
  })

  it('delivers messages to subscribers in sequence order despite jitter', async () => {
    const { client, latest } = makeHarness()
    const received: number[] = []
    client.onMessage((m) => received.push(m.sequence))

    await client.connect()
    latest().serverOpen()

    // Arrive out of order: 1, 3, 2 -> delivered as 1, 2, 3.
    latest().serverMessage(serverMessage(1))
    latest().serverMessage(serverMessage(3))
    latest().serverMessage(serverMessage(2))

    expect(received).toEqual([1, 2, 3])
    expect(client.getLastSequence()).toBe(3)
  })
})
