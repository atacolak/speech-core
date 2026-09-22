import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ConversationDetail, ConversationSummary, ConversationTurn } from '@/lib/api'
import { fetchConversation, fetchConversations } from '@/lib/api'
import { ALIGNED_TITLE } from '@/lib/spoken-alignment'

const player = vi.hoisted(() => ({
  onPlayingChange: undefined as ((playing: boolean) => void) | undefined,
  onPlayheadChange: undefined as ((seconds: number) => void) | undefined,
}))

vi.mock('@/components/take-player', () => ({
  TakePlayer: (props: {
    autoplay: boolean
    live: boolean
    onPlayingChange?: (playing: boolean) => void
    onPlayheadChange?: (seconds: number) => void
  }) => {
    player.onPlayingChange = props.onPlayingChange
    player.onPlayheadChange = props.onPlayheadChange
    return <div data-testid="take-player" data-autoplay={String(props.autoplay)} data-live={String(props.live)} />
  },
}))

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    fetchConversations: vi.fn(),
    fetchConversation: vi.fn(),
  }
})

import { ConversationsPane } from '@/features/conversations/conversations-pane'

const turn = (over: Partial<ConversationTurn>): ConversationTurn => ({
  id: 'ct_x',
  conversation_id: 'cv_1',
  msg_seq: 0,
  variation_seq: 0,
  role: 'assistant',
  text: '',
  audio_artifact_id: null,
  voice_id: 'v1',
  steer: null,
  generation: null,
  alignment: null,
  chosen: true,
  started_at: null,
  ended_at: null,
  ...over,
})

const SUMMARY: ConversationSummary = {
  id: 'cv_1',
  started_at: '2026-09-21T12:00:00Z',
  ended_at: '2026-09-21T12:05:00Z',
  saved: false,
  turn_count: 2,
}

function wav(): ArrayBuffer {
  const samples = new Int16Array([0, 0, 0, 0])
  const bytes = new Uint8Array(44 + samples.byteLength)
  const view = new DataView(bytes.buffer)
  const ascii = (offset: number, text: string) => {
    for (let index = 0; index < text.length; index += 1) {
      bytes[offset + index] = text.charCodeAt(index)
    }
  }
  ascii(0, 'RIFF')
  view.setUint32(4, bytes.byteLength - 8, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, 24_000, true)
  view.setUint32(28, 48_000, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  ascii(36, 'data')
  view.setUint32(40, samples.byteLength, true)
  bytes.set(new Uint8Array(samples.buffer), 44)
  return bytes.buffer
}

function detail(turns: ConversationTurn[]): ConversationDetail {
  return { ...SUMMARY, turns }
}

function renderPane(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ConversationsPane />
    </QueryClientProvider>,
  )
}

async function openSession(): Promise<void> {
  fireEvent.click(await screen.findByRole('button', { name: /cv_1/ }))
}

describe('conversations pane', () => {
  beforeEach(() => {
    player.onPlayingChange = undefined
    player.onPlayheadChange = undefined
    vi.mocked(fetchConversations).mockResolvedValue([SUMMARY])
    vi.mocked(fetchConversation).mockResolvedValue(detail([]))
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo) => {
        if (String(input).includes('/audio')) {
          return new Response(wav(), { status: 200 })
        }
        return new Response('missing', { status: 404 })
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders user and assistant turns in msg_seq order', async () => {
    vi.mocked(fetchConversation).mockResolvedValue(
      detail([
        turn({
          id: 'ct_user',
          msg_seq: 0,
          role: 'user',
          text: 'user said this',
          voice_id: null,
          audio_artifact_id: null,
        }),
        turn({
          id: 'ct_asst',
          msg_seq: 1,
          text: 'assistant said that',
          audio_artifact_id: 'art_1',
        }),
      ]),
    )
    renderPane()
    await openSession()
    const user = await screen.findByText('user said this')
    const assistant = screen.getByText('assistant said that')
    expect(user.compareDocumentPosition(assistant) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('renders a transcript-only user turn without a player', async () => {
    vi.mocked(fetchConversation).mockResolvedValue(
      detail([
        turn({
          id: 'ct_user',
          msg_seq: 0,
          role: 'user',
          text: 'just a transcript',
          voice_id: null,
          audio_artifact_id: null,
        }),
        turn({
          id: 'ct_asst',
          msg_seq: 1,
          text: 'with audio',
          audio_artifact_id: 'art_1',
        }),
      ]),
    )
    renderPane()
    await openSession()
    const user = await screen.findByRole('article', { name: 'user turn' })
    expect(user).toHaveTextContent('just a transcript')
    expect(within(user).queryByTestId('take-player')).toBeNull()
    expect(await screen.findByTestId('take-player')).toBeInTheDocument()
  })

  it('shows a voice-change marker between assistant turns with different voices', async () => {
    vi.mocked(fetchConversation).mockResolvedValue(
      detail([
        turn({ id: 'ct_a', msg_seq: 0, text: 'hello from v1', voice_id: 'v1', audio_artifact_id: 'art_a' }),
        turn({
          id: 'ct_u',
          msg_seq: 1,
          role: 'user',
          text: 'and you',
          voice_id: null,
          audio_artifact_id: null,
        }),
        turn({ id: 'ct_c', msg_seq: 2, text: 'hello from v2', voice_id: 'v2', audio_artifact_id: 'art_c' }),
      ]),
    )
    renderPane()
    await openSession()
    const first = await screen.findByText('hello from v1')
    const marker = await screen.findByText(/voice changed/i)
    const second = screen.getByText('hello from v2')
    expect(first.compareDocumentPosition(marker) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(marker.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows the spoken-word box only while that turn is playing', async () => {
    vi.mocked(fetchConversation).mockResolvedValue(
      detail([
        turn({
          id: 'ct_asst',
          msg_seq: 0,
          text: 'hello world',
          audio_artifact_id: 'art_1',
          alignment: {
            status: 'ready',
            text: 'hello world',
            words: [
              { text: 'hello', start_s: 0, end_s: 1 },
              { text: 'world', start_s: 1, end_s: 2 },
            ],
          },
        }),
      ]),
    )
    renderPane()
    await openSession()
    await screen.findByTestId('take-player')
    expect(document.querySelector('mark')).toBeNull()

    act(() => {
      player.onPlayingChange?.(true)
      player.onPlayheadChange?.(0.2)
    })
    const mark = await screen.findByTitle(ALIGNED_TITLE)
    expect(mark.tagName).toBe('MARK')
    expect(mark).toHaveTextContent('hello')

    act(() => {
      player.onPlayingChange?.(false)
    })
    expect(document.querySelector('mark')).toBeNull()
  })
})
