import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ConversationDetail, ConversationSummary, ConversationTurn, RuntimeInfo } from '@/lib/api'
import {
  ApiError,
  chooseTurnVariation,
  fetchConversation,
  fetchConversations,
  fetchRuntime,
  putConversationRingLimit,
  regenerateTurn,
  saveConversation,
  saveTurnVariation,
} from '@/lib/api'
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
    fetchRuntime: vi.fn(),
    chooseTurnVariation: vi.fn(),
    regenerateTurn: vi.fn(),
    saveTurnVariation: vi.fn(),
    saveConversation: vi.fn(),
    putConversationRingLimit: vi.fn(),
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

const RUNTIME: RuntimeInfo = {
  selected: 'E2',
  status: 'unloaded',
  state: 'unloaded',
  leftover_parked: false,
  not_a_pin_swap: true,
  voicecat_path: true,
  live_call_active: false,
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
    vi.mocked(fetchRuntime).mockResolvedValue(RUNTIME)
    vi.mocked(chooseTurnVariation).mockResolvedValue(turn({}))
    vi.mocked(regenerateTurn).mockResolvedValue(turn({}))
    vi.mocked(saveTurnVariation).mockResolvedValue({ run_id: 'run_1' })
    vi.mocked(saveConversation).mockResolvedValue({ ...SUMMARY, saved: true })
    vi.mocked(putConversationRingLimit).mockResolvedValue({ ring_limit: 3 })
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

  it('selecting a variation posts choose and re-renders chosen', async () => {
    let chosenId = 'ct_v0'
    const variations = (): ConversationTurn[] => [
      turn({
        id: 'ct_v0',
        msg_seq: 0,
        variation_seq: 0,
        text: 'first take',
        chosen: chosenId === 'ct_v0',
        audio_artifact_id: 'art_1',
      }),
      turn({
        id: 'ct_v1',
        msg_seq: 0,
        variation_seq: 1,
        text: 'second take',
        chosen: chosenId === 'ct_v1',
        audio_artifact_id: 'art_2',
      }),
    ]
    vi.mocked(fetchConversation).mockImplementation(async () => detail(variations()))
    vi.mocked(chooseTurnVariation).mockImplementation(async (_conversationId, turnId) => {
      chosenId = turnId
      return variations().find((row) => row.id === turnId)!
    })
    renderPane()
    await openSession()
    expect(await screen.findByRole('button', { name: 'V1' })).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('button', { name: 'V2' })).not.toHaveAttribute('aria-current')
    fireEvent.click(screen.getByRole('button', { name: 'V2' }))
    await waitFor(() => {
      expect(chooseTurnVariation).toHaveBeenCalledWith('cv_1', 'ct_v1')
    })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'V2' })).toHaveAttribute('aria-current', 'true')
    })
  })

  it('regenerate is disabled while live_call_active', async () => {
    vi.mocked(fetchRuntime).mockResolvedValue({ ...RUNTIME, live_call_active: true })
    vi.mocked(fetchConversation).mockResolvedValue(
      detail([turn({ id: 'ct_asst', msg_seq: 0, text: 'hello', audio_artifact_id: 'art_1' })]),
    )
    renderPane()
    await openSession()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /regenerate/i })).toBeDisabled()
    })

    cleanup()
    vi.mocked(fetchRuntime).mockResolvedValue({ ...RUNTIME, live_call_active: false })
    renderPane()
    await openSession()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /regenerate/i })).toBeEnabled()
    })
  })

  it('regenerate surfaces the 409 live_call_active message', async () => {
    vi.mocked(fetchRuntime).mockResolvedValue({ ...RUNTIME, live_call_active: false })
    vi.mocked(fetchConversation).mockResolvedValue(
      detail([turn({ id: 'ct_asst', msg_seq: 0, text: 'hello', audio_artifact_id: 'art_1' })]),
    )
    vi.mocked(regenerateTurn).mockRejectedValue(
      new ApiError('conversations', 409, {
        code: 'live_call_active',
        message: 'live call owns the engine',
      }),
    )
    renderPane()
    await openSession()
    fireEvent.click(await screen.findByRole('button', { name: /regenerate/i }))
    expect(await screen.findByText('live call owns the engine')).toBeInTheDocument()
  })

  it('save to voice posts the variation save and save posts the conversation save', async () => {
    vi.mocked(fetchConversation).mockResolvedValue(
      detail([turn({ id: 'ct_asst', msg_seq: 0, text: 'hello', audio_artifact_id: 'art_1' })]),
    )
    renderPane()
    await openSession()
    fireEvent.click(await screen.findByRole('button', { name: 'Save to voice' }))
    await waitFor(() => {
      expect(saveTurnVariation).toHaveBeenCalledWith('cv_1', 'ct_asst')
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => {
      expect(saveConversation).toHaveBeenCalledWith('cv_1')
    })
  })

  it('ring limit input puts the clamped value', async () => {
    vi.mocked(putConversationRingLimit).mockResolvedValue({ ring_limit: 50 })
    renderPane()
    const input = await screen.findByRole('spinbutton', { name: /ring/i })
    fireEvent.change(input, { target: { value: '51' } })
    await waitFor(() => {
      expect(putConversationRingLimit).toHaveBeenCalledWith(51)
    })
    await waitFor(() => {
      expect(input).toHaveValue(50)
    })
  })

  it('says so when the selected session has no turns', async () => {
    renderPane()
    expect(await screen.findByText(/no turns in this session/i)).toBeInTheDocument()
  })
})
