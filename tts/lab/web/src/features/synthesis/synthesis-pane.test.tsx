import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import type { Mock } from 'vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SynthesisPane } from '@/features/synthesis/synthesis-pane'
import type { GenerateJob, GenerateSegment } from '@/lib/api'
import { DEFAULT_GENERATION } from '@/lib/generation'
import { useWorkspace } from '@/state/workspace'

const mocks = vi.hoisted(() => ({
  enqueue: vi.fn(async (_index: number, _wavUrl: string) => {}),
  stop: vi.fn(),
  ended: undefined as ((index: number) => void) | undefined,
}))

vi.mock('@/features/synthesis/progressive', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/synthesis/progressive')>()
  class MockProgressivePlayer {
    constructor(_context?: AudioContext, onEnded?: (index: number) => void) {
      mocks.ended = onEnded
    }
    enqueue(index: number, wavUrl: string) {
      return mocks.enqueue(index, wavUrl)
    }
    stop() {
      mocks.stop()
    }
  }
  return { ...actual, ProgressivePlayer: MockProgressivePlayer }
})

const RUNTIME = { state: 'ready', live_call_active: false }

const VOICES = [
  { id: 'vp1', name: 'ata', tags: [] },
  { id: 'vp2', name: 'bex', tags: [] },
]

const TAKES = [
  {
    id: 'run_done',
    voice_id: 'vp1',
    output_artifact_id: 'art_done',
    latency_ms: 900,
    first_audio_ms: 320,
    duration_s: 42,
    rating: null,
    tags: [],
  },
]

function segment(index: number, state: GenerateSegment['state']): GenerateSegment {
  return {
    index,
    text: `segment ${index}`,
    state,
    duration_s: state === 'generated' ? 1 : null,
    audio_url: state === 'generated' ? `/api/generate/job1/segments/${index}/audio` : null,
  }
}

function job(overrides: Partial<GenerateJob> = {}): GenerateJob {
  return {
    id: 'job1',
    state: 'running',
    cursor: -1,
    lookahead: 2,
    blocked_on_live_call: false,
    run_id: null,
    output_artifact_id: null,
    error: null,
    segments: [
      segment(0, 'generated'),
      segment(1, 'generating'),
      segment(2, 'queued'),
      segment(3, 'pending'),
    ],
    ...overrides,
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * Stubs the whole lab surface. `polls` is the status body sequence the pane
 * sees after its initial status fetch; the start response is always a fresh
 * running job, as the server answers it.
 */
function stubLab(polls: GenerateJob[] = [job()], started: GenerateJob = job({ segments: [segment(0, 'queued'), segment(1, 'pending')] })) {
  let poll = 0
  const fetchMock = vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    if (url.includes('/api/runtime')) {
      return json(RUNTIME)
    }
    if (url.includes('/api/voices')) {
      return json({ items: VOICES })
    }
    if (url.includes('/api/runs')) {
      return json({ items: TAKES })
    }
    if (url.includes('/api/fixtures/steer')) {
      return json({ items: [] })
    }
    if (url.endsWith('/api/generate') && method === 'POST') {
      return json(started)
    }
    if (url.includes('/cancel')) {
      return json(
        job({
          state: 'cancelled',
          segments: [
            segment(0, 'generated'),
            segment(1, 'cancelled'),
            segment(2, 'pending'),
            segment(3, 'pending'),
          ],
        }),
      )
    }
    if (url.includes('/cursor')) {
      return json(job())
    }
    if (url.includes('/segments/')) {
      return new Response(new Uint8Array([0, 1]).buffer, { status: 200 })
    }
    if (url.includes('/api/generate/')) {
      const body = polls[Math.min(poll, polls.length - 1)]
      poll += 1
      return json(body)
    }
    return json({ detail: 'missing' }, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPane(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useWorkspace.setState({
    selectedVoiceId: 'vp1',
    selectedRunId: null,
    text: 'hello world',
    steer: 'dry',
    generation: { ...DEFAULT_GENERATION },
  })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

function callsTo(fetchMock: Mock, fragment: string) {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes(fragment))
}

async function pressGenerate() {
  fireEvent.click(await screen.findByRole('button', { name: 'Generate' }))
}

describe('GENERATE play-as-you-go', () => {
  beforeEach(() => {
    mocks.enqueue.mockClear()
    mocks.stop.mockClear()
    mocks.ended = undefined
  })

  afterEach(() => {
    // The stub stays installed: panes refetch during teardown, and a relative URL
    // reaching the real fetch would throw after the run.
    useWorkspace.setState({ selectedVoiceId: null, selectedRunId: null })
  })

  it('plays the first generated segment while the job is running', async () => {
    const fetchMock = stubLab()
    renderPane(<SynthesisPane />)
    await pressGenerate()
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/generate'),
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(await screen.findByText('generated')).toBeInTheDocument()
    expect(screen.getByText('generating')).toBeInTheDocument()
    expect(screen.getByText('queued')).toBeInTheDocument()
    expect(screen.getByText('pending')).toBeInTheDocument()
    await waitFor(() =>
      expect(mocks.enqueue).toHaveBeenCalledWith(
        0,
        expect.stringContaining('/segments/0/audio'),
      ),
    )
    // A whole-document wait would post one synthesize and produce no per-segment audio.
    expect(callsTo(fetchMock, '/api/synthesize')).toHaveLength(0)

    // The caret only moves once the scheduled segment actually finishes.
    expect(callsTo(fetchMock, '/cursor')).toHaveLength(0)
    act(() => mocks.ended?.(0))
    await waitFor(() => expect(callsTo(fetchMock, '/cursor')).toHaveLength(1))
    expect(callsTo(fetchMock, '/cursor')[0][1]).toMatchObject({ method: 'POST' })
  })

  it('cancels pending work without stopping produced playback', async () => {
    const fetchMock = stubLab()
    renderPane(<SynthesisPane />)
    await pressGenerate()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/cancel'),
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(mocks.stop).not.toHaveBeenCalled()
    expect(screen.getByText('generated')).toBeInTheDocument()
  })

  it('invalidates the old snapshot when text changes', async () => {
    const fetchMock = stubLab()
    renderPane(<SynthesisPane />)
    await pressGenerate()
    fireEvent.change(await screen.findByLabelText('Say'), { target: { value: 'hello edited' } })
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/cancel'),
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(callsTo(fetchMock, '/cancel')).toHaveLength(1)
  })

  it.each([
    ['delivery', () => fireEvent.change(screen.getByLabelText('Delivery'), { target: { value: 'changed' } })],
    ['generation setting', () => act(() => useWorkspace.getState().patchGeneration({ cfg: 2 }))],
    ['selected voice', () => act(() => useWorkspace.getState().selectVoice('vp2'))],
  ])('invalidates the old snapshot when %s changes', async (_name, change) => {
    const fetchMock = stubLab()
    renderPane(<SynthesisPane />)
    await pressGenerate()
    change()
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/cancel'),
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(mocks.stop).not.toHaveBeenCalled()
  })

  it('selects the composed run and refreshes takes on the complete poll', async () => {
    const fetchMock = stubLab([
      job({
        state: 'complete',
        cursor: 3,
        run_id: 'run_done',
        output_artifact_id: 'art_done',
        segments: [
          segment(0, 'generated'),
          segment(1, 'generated'),
          segment(2, 'generated'),
          segment(3, 'generated'),
        ],
      }),
    ])
    renderPane(<SynthesisPane />)
    await pressGenerate()
    await waitFor(() => expect(callsTo(fetchMock, '/api/runs').length).toBeGreaterThan(1))
    expect(useWorkspace.getState().selectedRunId).toBe('run_done')
    const audio = await waitFor(() => {
      const node = document.querySelector('audio')
      expect(node?.getAttribute('src')).toContain('art_done')
      return node as HTMLAudioElement
    })
    expect(audio.getAttribute('src')).toContain('art_done')
    expect(await screen.findByRole('link', { name: /Download/ })).toHaveAttribute(
      'href',
      expect.stringContaining('art_done'),
    )
  })
})
