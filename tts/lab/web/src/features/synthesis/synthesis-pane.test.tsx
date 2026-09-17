import { setTimeout as delay } from 'node:timers/promises'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactElement } from 'react'
import type { Mock } from 'vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppShell } from '@/components/app-shell'
import { SynthesisPane } from '@/features/synthesis/synthesis-pane'
import { DEFAULT_GENERATION } from '@/lib/generation'
import { ALIGNED_TITLE, ESTIMATED_TITLE } from '@/lib/spoken-alignment'
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

const SAY = 'one verylongword three'

/** Deliberately uneven Parakeet cadence: the second word only starts at 4.5 s. */
const ALIGNED_WORDS = [
  { text: 'one', start_s: 0, end_s: 4.4 },
  { text: 'verylongword', start_s: 4.5, end_s: 5.0 },
  { text: 'three', start_s: 5.1, end_s: 6.0 },
]

const RUN_A = {
  id: 'run_a',
  voice_id: 'vp1',
  output_artifact_id: 'art_a',
  latency_ms: 800,
  first_audio_ms: 210,
  duration_s: 6,
  rating: null,
  tags: [],
  request_snapshot: { text: SAY, produced_text: SAY, stopped: false },
  alignment: { status: 'ready', text: SAY, words: ALIGNED_WORDS },
}

const RUN_B = {
  id: 'run_b',
  voice_id: 'vp2',
  output_artifact_id: 'art_b',
  latency_ms: 900,
  first_audio_ms: 240,
  duration_s: 2,
  rating: null,
  tags: [],
}

/** The newest take before Parakeet has heard it. */
const RUN_A_PENDING = { ...RUN_A, alignment: { status: 'pending' } }

/** An older aligned take: the only measured speech rate that may back a fallback. */
const RUN_PRIOR = {
  id: 'run_prior',
  voice_id: 'vp1',
  output_artifact_id: 'art_prior',
  latency_ms: 700,
  first_audio_ms: 180,
  duration_s: 2,
  rating: null,
  tags: [],
  request_snapshot: { text: 'four five six', produced_text: 'four five six', stopped: false },
  alignment: {
    status: 'ready',
    text: 'four five six',
    words: [
      { text: 'four', start_s: 0, end_s: 1 },
      { text: 'five', start_s: 1, end_s: 1.5 },
      { text: 'six', start_s: 1.5, end_s: 2 },
    ],
  },
}

const RUNS = [RUN_A, RUN_B]
const RUN_NEWEST_FOR_VP1 = { ...RUN_A, id: 'run_newest', output_artifact_id: 'art_newest' }

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
  /** Swap the `/api/runs` payload so polling can be watched across a flip. */
  setRuns: (items: unknown[]) => void
}

/**
 * Stubs the whole lab surface. `/api/generate/stream` hands back a controlled
 * raw-PCM body: a test enqueues chunks itself and closes it during cleanup, so
 * the stream stays open across Stop exactly as the server keeps it open while
 * it finalizes the take.
 */
function stubLab(initialRuns: unknown[] = RUNS): Lab {
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null
  let closed = false
  let runs: unknown[] = initialRuns
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
      return json({ items: runs })
    }
    if (url.endsWith('/api/fixtures/steers')) {
      return json({
        items: [
          {
            id: 'neutral-grounded',
            title: 'neutral / grounded',
            text: "Okay, I've gone through everything. Here's what I think we should do next.",
            steer:
              'Natural conversational delivery. Calm, grounded, matter-of-fact, with an even pace and restrained expression.',
          },
        ],
      })
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
      return new Response(wav(6), { status: 200 })
    }
    if (url.includes('/api/artifacts/art_newest/audio')) {
      return new Response(wav(6), { status: 200 })
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
    setRuns: (items) => {
      runs = items
    },
  }
}

function renderPane(node: ReactElement, text = 'hello world') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useWorkspace.setState({
    selectedVoiceId: 'vp1',
    selectedRunId: null,
    text,
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

/** Manual animation frames: the player's clock is driven, never polled. */
let frames = new Map<number, FrameRequestCallback>()
let nextFrameId = 0

function tick() {
  const pending = frames.entries().next().value
  if (pending === undefined) {
    return
  }
  frames.delete(pending[0])
  act(() => {
    pending[1](performance.now())
  })
}

function audio() {
  const context = FakeAudioContext.contexts.at(-1)
  if (context === undefined) {
    throw new Error('no AudioContext was created')
  }
  return context
}

function waveSlider() {
  return screen.getByRole('slider', { name: 'Waveform' })
}

describe('GENERATE take player', () => {
  let lab: Lab

  beforeEach(() => {
    FakeAudioContext.contexts.length = 0
    FakeAudioContext.sources.length = 0
    frames = new Map()
    nextFrameId = 0
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      nextFrameId += 1
      frames.set(nextFrameId, callback)
      return nextFrameId
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      frames.delete(id)
    })
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
    await waitFor(() => expect(waveSlider().getAttribute('aria-valuemax')).toBe('1'))
    // Autoplay is the only way a 1 s take is not silent.
    expect(FakeAudioContext.sources.map((source) => source.started)).toEqual([[{ when: 0, offset: 0 }]])
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument()

    // Later PCM grows the same buffer instead of a second player.
    act(() => lab.push(pcmChunk(1)))
    await waitFor(() => expect(waveSlider().getAttribute('aria-valuemax')).toBe('2'))
    expect(document.querySelectorAll('audio')).toHaveLength(1)
    expect(FakeAudioContext.sources).toHaveLength(1)
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

  it('loads titled fixtures from the plural path and applies one', async () => {
    renderPane(<SynthesisPane />)
    const picker = await screen.findByLabelText('Fixture')
    expect(picker.tagName).toBe('SELECT')
    expect(within(picker).getByText('neutral / grounded')).toBeInTheDocument()
    fireEvent.change(picker, { target: { value: 'neutral-grounded' } })
    expect(screen.getByLabelText('Say')).toHaveValue(
      "Okay, I've gone through everything. Here's what I think we should do next.",
    )
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

  it('restores each selected voice latest take with its card audio', async () => {
    renderPane(<SynthesisPane />)
    await waitFor(() =>
      expect(callsTo(lab.fetchMock, '/api/artifacts/art_a/audio').length).toBeGreaterThan(0),
    )
    expect(screen.getByLabelText('Waveform')).toBeInTheDocument()
    expect(document.querySelectorAll('audio')).toHaveLength(1)
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
    expect(document.querySelectorAll('audio')).toHaveLength(1)
    expect(screen.getByRole('link', { name: /Download/ })).toHaveAttribute(
      'href',
      expect.stringContaining('art_b'),
    )
  })
  it("follows the newest returned take when it differs from the voice's pointer", async () => {
    lab.close()
    lab = stubLab([RUN_NEWEST_FOR_VP1, RUN_A])
    renderPane(<SynthesisPane />)

    await waitFor(() =>
      expect(callsTo(lab.fetchMock, '/api/artifacts/art_newest/audio').length).toBeGreaterThan(0),
    )
    expect(within(screen.getByRole('group', { name: 'Latest take' })).getByRole('link', { name: /Download/ })).toHaveAttribute(
      'href',
      expect.stringContaining('art_newest'),
    )
  })

  it('sets 17px Say and Delivery without dictionary marks', async () => {
    renderPane(<SynthesisPane />)
    const say = await screen.findByLabelText('Say')
    const delivery = screen.getByLabelText('Delivery')
    for (const node of [say, delivery]) {
      expect(node).toHaveClass('text-[17px]')
      expect(node).toHaveAttribute('spellcheck', 'false')
      expect(node).toHaveAttribute('autocomplete', 'off')
      expect(node).toHaveAttribute('autocorrect', 'off')
      expect(node).toHaveAttribute('autocapitalize', 'off')
    }
    const mirror = say.parentElement?.querySelector('[aria-hidden="true"]')
    expect(mirror).toHaveClass('text-[17px]')
  })

  it('highlights the aligned Say word at the audible playhead', async () => {
    renderPane(<SynthesisPane />, SAY)
    await screen.findByLabelText('Waveform')
    expect(screen.getByLabelText('Say')).toHaveValue(SAY)

    fireEvent.click(await screen.findByRole('button', { name: 'Play' }))
    // Two seconds into a take whose second word only begins at 4.5 s: a
    // proportional estimator would light "verylongword", so only the real
    // Parakeet interval can put the mark on "one".
    audio().currentTime = 2
    tick()

    await waitFor(() => expect(waveSlider().getAttribute('aria-valuenow')).toBe('2'))
    const mark = await screen.findByTitle(ALIGNED_TITLE)
    expect(mark.tagName).toBe('MARK')
    expect(mark.textContent).toBe('one')
    expect(mark.getAttribute('title')).toContain('Parakeet')
    expect(mark.closest('[aria-hidden="true"]')).not.toBeNull()
    expect(document.querySelectorAll('mark')).toHaveLength(1)
    // A blue wash plus a 1px ring: the mark is the spoken word, not a cursor block.
    expect(mark.className).toMatch(/38bdf8|sky-400|56,\s*189,\s*248/)
    expect(mark.className).toContain('shadow-[0_0_0_1px')
    expect(mark).not.toHaveClass('bg-zinc-100/15')
  })

  it('labels prior-rate fallback and renders no fallback without a rate', async () => {
    lab.setRuns([RUN_A_PENDING, RUN_PRIOR])
    const view = renderPane(<SynthesisPane />, SAY)
    await screen.findByLabelText('Waveform')

    fireEvent.click(await screen.findByRole('button', { name: 'Play' }))
    // 5.5 measured characters per second puts the estimate on the second word
    // where the real alignment puts it on the first: the two are not the same claim.
    audio().currentTime = 2
    tick()

    const mark = await screen.findByTitle(ESTIMATED_TITLE)
    expect(mark.textContent).toBe('verylongword')
    expect(mark.getAttribute('title')).toContain('error may span the whole take')

    // No aligned take has ever measured a rate for this voice: invent nothing.
    view.unmount()
    lab.setRuns([RUN_A_PENDING])
    renderPane(<SynthesisPane />, SAY)
    await screen.findByLabelText('Waveform')
    fireEvent.click(await screen.findByRole('button', { name: 'Play' }))
    audio().currentTime = 2
    tick()
    await waitFor(() => expect(waveSlider().getAttribute('aria-valuenow')).toBe('2'))
    expect(document.querySelector('mark')).toBeNull()
  })

  it('refetches only while latest alignment is pending', async () => {
    lab.setRuns([RUN_A_PENDING])
    renderPane(<SynthesisPane />, SAY)
    const runsCalls = () => callsTo(lab.fetchMock, '/api/runs').length
    await waitFor(() => expect(runsCalls()).toBeGreaterThanOrEqual(2), { timeout: 3000 })

    lab.setRuns([RUN_A])
    await waitFor(() => expect(runsCalls()).toBeGreaterThanOrEqual(3), { timeout: 3000 })
    const settled = runsCalls()
    // Well past the pane's one-second alignment poll: a settled take stays quiet.
    await act(async () => {
      await delay(1400)
    })
    expect(runsCalls()).toBe(settled)
  })

  it('transport never posts Stop and stream keeps appending', async () => {
    renderPane(<SynthesisPane />)
    fireEvent.click(await screen.findByRole('button', { name: 'Generate' }))
    await waitFor(() => expect(callsTo(lab.fetchMock, '/api/generate/stream')).toHaveLength(1))
    act(() => lab.push(pcmChunk(1)))
    await waitFor(() => expect(waveSlider().getAttribute('aria-valuemax')).toBe('1'))

    fireEvent.click(await screen.findByRole('button', { name: 'Pause' }))
    fireEvent.click(screen.getByRole('button', { name: 'Back 5 seconds' }))
    fireEvent.click(screen.getByRole('button', { name: 'Forward 5 seconds' }))

    expect(callsTo(lab.fetchMock, '/stop')).toHaveLength(0)
    act(() => lab.push(pcmChunk(1)))
    await waitFor(() => expect(waveSlider().getAttribute('aria-valuemax')).toBe('2'))
    expect(callsTo(lab.fetchMock, '/stop')).toHaveLength(0)
    expect(await screen.findByRole('button', { name: 'Stop' })).toBeInTheDocument()
  })
  it('opens Settings on GENERATE load without Voice or Reference blocks', async () => {
    useWorkspace.setState({ settingsOpen: true })
    renderPane(<AppShell />)
    expect(await screen.findByRole('heading', { name: 'Settings' })).toBeInTheDocument()
    const settings = screen.getByRole('heading', { name: 'Settings' }).closest('section')
    expect(within(settings!).queryByText(/^Voice$/)).toBeNull()
    expect(within(settings!).queryByText(/^Reference$/)).toBeNull()
    expect(screen.getByRole('heading', { name: 'Voices' })).toBeInTheDocument()
  })

  it('renders darker joined GENERATE chrome', async () => {
    useWorkspace.setState({ settingsOpen: true })
    renderPane(<AppShell />)
    const panels = await waitFor(() => {
      // Panels keep their size on the outer element and their skin on the inner one.
      const found = document.querySelectorAll('[data-panel] > div')
      expect(found).toHaveLength(3)
      return found
    })
    expect(panels[1]).toHaveClass('bg-zinc-900')
    expect(panels[0]).toHaveClass('bg-zinc-850')
    expect(panels[2]).toHaveClass('bg-zinc-850')

    const say = await screen.findByLabelText('Say')
    expect(say.closest('section')).toHaveClass('bg-zinc-900')
    expect(say).toHaveClass('bg-zinc-950/40', 'border-zinc-800')
    expect(say).not.toHaveClass('bg-zinc-950')
    expect(screen.getByLabelText('Delivery')).toHaveClass('bg-zinc-950/40', 'border-zinc-800')
    act(() => useWorkspace.setState({ settingsOpen: false }))
  })
})
