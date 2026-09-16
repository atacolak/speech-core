import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import type { Mock } from 'vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SynthesisPane } from '@/features/synthesis/synthesis-pane'
import { DEFAULT_GENERATION } from '@/lib/generation'
import { useWorkspace } from '@/state/workspace'

const RUNTIME = { state: 'ready', live_call_active: false }
const SAMPLE_RATE = 24000

/** Two voices whose stored generation and latest take both differ. */
const VOICES = [
  {
    id: 'vp1',
    name: 'ata',
    tags: [],
    latest_take_id: 'run_a',
    generation: { guidance: { mode: 'single', cfg: 1 }, seed: 7 },
  },
  {
    id: 'vp2',
    name: 'bex',
    tags: [],
    latest_take_id: 'run_b',
    generation: { guidance: { mode: 'single', cfg: 3 }, seed: 9 },
  },
]

const RUNS = [
  {
    id: 'run_a',
    voice_id: 'vp1',
    output_artifact_id: 'art_a',
    latency_ms: 800,
    first_audio_ms: 210,
    duration_s: 1,
    rating: null,
    tags: [],
  },
  {
    id: 'run_b',
    voice_id: 'vp2',
    output_artifact_id: 'art_b',
    latency_ms: 900,
    first_audio_ms: 240,
    duration_s: 2,
    rating: null,
    tags: [],
  },
]

type Started = { when: number; offset: number }

class FakeSource {
  buffer: AudioBuffer | null = null
  readonly started: Started[] = []
  stopped = false
  onended: (() => void) | null = null

  connect() {}
  disconnect() {}

  start(when: number, offset?: number) {
    this.started.push({ when, offset: offset ?? 0 })
  }

  stop() {
    this.stopped = true
  }
}

class FakeAudioContext {
  static readonly contexts: FakeAudioContext[] = []
  static readonly sources: FakeSource[] = []

  readonly destination = {}
  currentTime = 0
  closed = false

  constructor() {
    FakeAudioContext.contexts.push(this)
  }

  createBuffer(_channels: number, length: number, sampleRate: number) {
    const channel = new Float32Array(length)
    return {
      length,
      sampleRate,
      duration: length / sampleRate,
      getChannelData: () => channel,
    } as unknown as AudioBuffer
  }

  createBufferSource() {
    const source = new FakeSource()
    FakeAudioContext.sources.push(source)
    return source as unknown as AudioBufferSourceNode
  }

  resume() {
    return Promise.resolve()
  }

  close() {
    this.closed = true
    return Promise.resolve()
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** `seconds` of audible s16le mono, so the timeline duration is exact. */
function pcmChunk(seconds: number, fill = 8000): Uint8Array {
  const frames = Math.round(seconds * SAMPLE_RATE)
  const samples = new Int16Array(frames)
  samples.fill(fill)
  return new Uint8Array(samples.buffer)
}

/** A tiny valid mono 16-bit PCM WAV, as `/api/artifacts/{id}/audio` serves. */
function wav(seconds: number): ArrayBuffer {
  const samples = new Int16Array(Math.round(seconds * SAMPLE_RATE))
  samples.fill(4000)
  const bytes = new Uint8Array(44 + samples.byteLength)
  const view = new DataView(bytes.buffer)
  view.setUint32(0, 0x52494646, false)
  view.setUint32(4, 36 + samples.byteLength, true)
  view.setUint32(8, 0x57415645, false)
  view.setUint32(12, 0x666d7420, false)
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, SAMPLE_RATE, true)
  view.setUint32(28, SAMPLE_RATE * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  view.setUint32(36, 0x64617461, false)
  view.setUint32(40, samples.byteLength, true)
  bytes.set(new Uint8Array(samples.buffer), 44)
  return bytes.buffer
}

type Lab = {
  fetchMock: Mock
  push: (bytes: Uint8Array) => void
  close: () => void
}

/**
 * Stubs the whole lab surface. `/api/generate/stream` hands back a controlled
 * raw-PCM body: a test enqueues chunks itself and closes it during cleanup, so
 * the stream stays open across Stop exactly as the server keeps it open while
 * it finalizes the take.
 */
function stubLab(): Lab {
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null
  let closed = false
  const body = new ReadableStream<Uint8Array>({
    start(next) {
      controller = next
    },
  })
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
      return json({ items: RUNS })
    }
    if (url.includes('/api/fixtures/steer')) {
      return json({ items: [] })
    }
    if (url.includes('/api/generate/stream') && method === 'POST') {
      return new Response(body, {
        status: 200,
        headers: { 'X-Generate-Id': 'gen_1', 'X-Sample-Rate': String(SAMPLE_RATE) },
      })
    }
    if (url.includes('/stop')) {
      return json({ id: 'gen_1', stopped: true })
    }
    if (url.includes('/api/artifacts/art_a/audio')) {
      return new Response(wav(1), { status: 200 })
    }
    if (url.includes('/api/artifacts/art_b/audio')) {
      return new Response(wav(2), { status: 200 })
    }
    return json({ detail: 'missing' }, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    fetchMock,
    push: (bytes) => {
      controller?.enqueue(bytes)
    },
    close: () => {
      if (closed) {
        return
      }
      closed = true
      try {
        controller?.close()
      } catch {
        // Already closed by the reader.
      }
    },
  }
}

function renderPane(node: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useWorkspace.setState({
    selectedVoiceId: 'vp1',
    selectedRunId: null,
    text: 'hello world',
    steer: 'dry',
    // Deliberately not the stored profile: hydration is what must fix it.
    generation: { ...DEFAULT_GENERATION, cfg: 4 },
  })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

function callsTo(fetchMock: Mock, fragment: string) {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes(fragment))
}

function postBody(fetchMock: Mock, fragment: string) {
  const call = callsTo(fetchMock, fragment)[0]
  return JSON.parse(String((call?.[1] as RequestInit | undefined)?.body)) as {
    generation: { guidance: unknown }
  }
}

describe('GENERATE take player', () => {
  let lab: Lab

  beforeEach(() => {
    FakeAudioContext.contexts.length = 0
    FakeAudioContext.sources.length = 0
    vi.stubGlobal('AudioContext', FakeAudioContext)
    lab = stubLab()
  })

  afterEach(() => {
    lab.close()
    // The stub stays installed: panes refetch during teardown, and a relative URL
    // reaching the real fetch would throw after the run.
    useWorkspace.setState({ selectedVoiceId: null, selectedRunId: null })
  })

  it('consumes first stream chunk into one player', async () => {
    renderPane(<SynthesisPane />)
    fireEvent.click(await screen.findByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(callsTo(lab.fetchMock, '/api/generate/stream')).toHaveLength(1))

    act(() => lab.push(pcmChunk(1)))
    await waitFor(() => expect(screen.getByRole('slider', { name: 'Seek' }).getAttribute('max')).toBe('1'))
    // Autoplay is the only way a 1 s take is not silent.
    expect(FakeAudioContext.sources.map((source) => source.started)).toEqual([[{ when: 0, offset: 0 }]])
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument()

    // Later PCM grows the same buffer instead of a second player.
    act(() => lab.push(pcmChunk(1)))
    await waitFor(() => expect(screen.getByRole('slider', { name: 'Seek' }).getAttribute('max')).toBe('2'))
    expect(screen.getAllByLabelText('Waveform')).toHaveLength(1)
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(document.querySelector('audio')).toBeNull()
  })

  it('posts Stop and never renders Cancel or segment states', async () => {
    renderPane(<SynthesisPane />)
    fireEvent.click(await screen.findByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(callsTo(lab.fetchMock, '/api/generate/stream')).toHaveLength(1))
    act(() => lab.push(pcmChunk(1)))

    expect(screen.queryByText(/generated|generating|queued|pending/i)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
    await waitFor(() =>
      expect(callsTo(lab.fetchMock, '/api/generate/gen_1/stop')).toHaveLength(1),
    )
    expect(callsTo(lab.fetchMock, '/api/generate/gen_1/stop')[0][1]).toMatchObject({
      method: 'POST',
    })
    // Stop ends unborn work; it never silences produced audio.
    expect(FakeAudioContext.sources.every((source) => !source.stopped)).toBe(true)
  })

  it('gives Say the min-h-70 instrument height', async () => {
    renderPane(<SynthesisPane />)
    const say = await screen.findByLabelText('Say')
    expect(say).toHaveClass('min-h-70')
    expect(say).not.toHaveClass('min-h-28')
  })

  it('hydrates stored cfg before first Generate', async () => {
    renderPane(<SynthesisPane />)
    await waitFor(() => expect(useWorkspace.getState().generation.cfg).toBe(1))
    // No Settings drawer is ever mounted here.
    expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(callsTo(lab.fetchMock, '/api/generate/stream')).toHaveLength(1))
    expect(postBody(lab.fetchMock, '/api/generate/stream').generation.guidance).toEqual({
      mode: 'single',
      cfg: 1,
    })
    expect(useWorkspace.getState().generation.seed).toBe(7)
  })

  it('restores each selected voice latest take without audio src', async () => {
    renderPane(<SynthesisPane />)
    await waitFor(() =>
      expect(callsTo(lab.fetchMock, '/api/artifacts/art_a/audio').length).toBeGreaterThan(0),
    )
    expect(screen.getByLabelText('Waveform')).toBeInTheDocument()
    expect(document.querySelector('audio')).toBeNull()
    expect(await screen.findByRole('link', { name: /Download/ })).toHaveAttribute(
      'href',
      expect.stringContaining('art_a'),
    )

    act(() => useWorkspace.getState().selectVoice('vp2'))
    await waitFor(() =>
      expect(callsTo(lab.fetchMock, '/api/artifacts/art_b/audio').length).toBeGreaterThan(0),
    )
    expect(callsTo(lab.fetchMock, '/api/artifacts/art_a/audio').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('Waveform')).toBeInTheDocument()
    expect(document.querySelector('audio')).toBeNull()
    expect(screen.getByRole('link', { name: /Download/ })).toHaveAttribute(
      'href',
      expect.stringContaining('art_b'),
    )
  })
})
