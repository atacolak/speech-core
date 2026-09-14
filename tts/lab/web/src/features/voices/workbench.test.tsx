import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Toaster } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppShell } from '@/components/app-shell'
import { VoicesPane } from '@/features/voices/voices-pane'
import { VoiceWorkbench } from '@/features/voices/workbench'
import type { Voice, VoiceArtifact } from '@/lib/api'
import { useWorkspace } from '@/state/workspace'

const h = vi.hoisted(() => ({
  regions: [] as Array<{
    id?: string
    start: number
    end: number
    color?: string
    remove?: () => void
  }>,
  plays: [] as Array<[number?, number?]>,
  duration: 12,
  handlers: {} as Record<string, Array<(...args: unknown[]) => void>>,
}))

vi.mock('wavesurfer.js', () => {
  class FakeWaveSurfer {
    static create() {
      return new FakeWaveSurfer()
    }
    on(event: string, callback: (...args: unknown[]) => void) {
      h.handlers[event] = [...(h.handlers[event] ?? []), callback]
    }
    getDuration() {
      return h.duration
    }
    play(start?: number, end?: number) {
      h.plays.push([start, end])
    }
    destroy() {}
  }
  return { default: FakeWaveSurfer }
})

vi.mock('wavesurfer.js/dist/plugins/regions.esm.js', () => {
  class FakeRegions {
    static create() {
      return new FakeRegions()
    }
    enableDragSelection() {}
    on() {}
    getRegions() {
      return h.regions
    }
    addRegion(options: { id?: string; start: number; end: number; color?: string }) {
      const region = {
        ...options,
        remove: () => {
          h.regions = h.regions.filter((item) => item !== region)
        },
      }
      h.regions.push(region)
      return region
    }
  }
  return { default: FakeRegions }
})

/** A live diarization result: parked from the default surface, never absent from the API. */
const ANALYSIS = {
  id: 'sa1',
  speakers: [
    { id: 'S1', label: 'Speaker 1', duration_s: 18.4 },
    { id: 'S2', label: 'Speaker 2', duration_s: 3 },
  ],
  segments: [
    { speaker_id: 'S1', start_s: 0, end_s: 2, text: 'one', overlap: false },
    { speaker_id: 'S2', start_s: 3, end_s: 6, text: 'two', overlap: false },
  ],
  overlaps: [{ start_s: 2, end_s: 3, speakers: ['S1', 'S2'] }],
  stale: false,
}

/** A generated Resemble denoise variant: kept in the API, parked from the default surface. */
const DENOISE_VARIANT = {
  id: 'rv_den',
  voice_profile_id: 'vp1',
  kind: 'resemble',
  audio_artifact_id: 'art_den',
  duration_s: 11.5,
  stale: false,
}

/** An AuK experiment: lineage-bearing, listed while it is still a candidate. */
function aukCandidate(overrides: Record<string, unknown> = {}) {
  return {
    id: 'rv_auk',
    voice_profile_id: 'vp1',
    kind: 'auk',
    audio_artifact_id: 'art_auk',
    duration_s: 12,
    auk_task: 'enhance',
    instruction: 'Remove noise and reverberation while retaining the speech.',
    model_variant: 'auk-base',
    auk_precision: 'bf16',
    encoder_precision: 'w4a8',
    seed: 7,
    settings: null,
    approved: false,
    stale: false,
    ...overrides,
  }
}

/** Today's 1:1 voice JSON: one source, variants, one active reference. */
function voice(overrides: Record<string, unknown> = {}): Voice {
  return {
    id: 'vp1',
    name: 'ata',
    tags: [],
    source_audio_artifact_id: 'art_src',
    original_artifact_id: 'art_src',
    original_format: 'wav',
    source_transcript: 'hello',
    keep_intervals: [{ start_s: 0, end_s: 12 }],
    effective_transcript: 'hello',
    active_reference_variant_id: 'rv_orig',
    active_variant: {
      id: 'rv_orig',
      voice_profile_id: 'vp1',
      kind: 'original',
      audio_artifact_id: 'art_src',
      duration_s: 12,
      stale: false,
    },
    variants: [],
    speaker_analysis: null,
    duration_s: 12,
    source_duration_s: 12,
    effective_duration_s: 12,
    created_at: '2026-09-12T00:00:00Z',
    updated_at: '2026-09-12T00:00:00Z',
    ...overrides,
  } as Voice
}

/** The profile shape t1 emits: many sources, role-carrying artifacts, one optional default. */
function profileVoice(overrides: Partial<Voice> = {}): Voice {
  return voice({
    sources: [
      {
        id: 'src_a',
        label: 'take A',
        artifact_id: 'art_src',
        transcript: 'hello',
        duration_s: 12,
        keep_intervals: [{ start_s: 2, end_s: 6 }],
      },
      {
        id: 'src_b',
        label: 'take B',
        artifact_id: 'art_src_b',
        transcript: 'hello again',
        duration_s: 9,
        keep_intervals: [{ start_s: 0, end_s: 9 }],
      },
    ],
    artifacts: [
      {
        id: 'a_exp',
        role: 'experiment',
        kind: 'auk',
        name: 'enhance 1',
        audio_artifact_id: 'art_exp',
        source_id: 'src_a',
        auk_task: 'enhance',
        instruction: 'Remove noise and reverberation while retaining the speech.',
        approved: false,
        default: false,
        stale: false,
      },
      {
        id: 'a_ref',
        role: 'reference',
        kind: 'auk',
        name: 'enhance 1 approved',
        audio_artifact_id: 'art_ref',
        parent_id: 'a_exp',
        source_id: 'src_a',
        auk_task: 'enhance',
        instruction: 'Remove noise and reverberation while retaining the speech.',
        approved: true,
        default: true,
        stale: false,
      },
    ],
    default_reference_id: 'a_ref',
    ...overrides,
  })
}

type Call = { url: string; method: string; body: unknown }

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockFetch(
  current: () => Array<Record<string, unknown>>,
  calls: Call[],
  runtime: Record<string, unknown> = {},
) {
  return vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body: Record<string, unknown> =
      typeof init?.body === 'string' ? JSON.parse(init.body) : {}
    calls.push({ url, method, body })
    if (url.includes('/reference/audio')) {
      return new Response(new Uint8Array([82, 73, 70, 70]), { status: 200 })
    }
    if (url.includes('/sources')) {
      return json({ detail: { code: 'not_found', message: 'no such route' } }, 404)
    }
    // Promotion of an existing experiment stays; nothing runs a processor to make a new one.
    if (url.includes('/auk/approve')) {
      return json(current()[0])
    }
    if (url.includes('/api/runtime')) {
      return json({
        state: 'unloaded',
        status: 'unloaded',
        selected: 'breeze-tts2',
        leftover_parked: true,
        not_a_pin_swap: true,
        voicecat_path: true,
        live_call_active: false,
        processor: null,
        ...runtime,
      })
    }
    if (url.includes('/api/voices')) {
      const listed = current()[0]
      return json(method !== 'GET' ? listed : { items: current() })
    }
    if (url.includes('/api/runs')) {
      return json({ items: [] })
    }
    if (url.includes('/api/fixtures/steers')) {
      return json({ items: [] })
    }
    return json({ detail: 'missing' }, 404)
  })
}

function renderWorkbench() {
  useWorkspace.setState({ selectedVoiceId: 'vp1', editorOpen: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <VoiceWorkbench />
      <Toaster />
    </QueryClientProvider>,
  )
}

function renderShell() {
  useWorkspace.setState({ selectedVoiceId: 'vp1', editorOpen: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AppShell />
    </QueryClientProvider>,
  )
}

function audioSources(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('audio')).map(
    (node) => node.getAttribute('src') ?? '',
  )
}

function findWorkbench() {
  return screen.findByRole('heading', { name: /Voice workbench/ })
}

const assetList = (name: string) => screen.getByRole('list', { name })
const compareRegion = () => screen.getByRole('region', { name: 'compare' })
const inspector = () => screen.getByRole('complementary', { name: 'inspector' })

/** Each render sets the workspace it needs; teardown only restores the real fetch. */
let calls: Call[] = []

beforeEach(() => {
  calls = []
  h.regions = []
  h.plays = []
  h.handlers = {}
})

/** Each render sets the workspace it needs; teardown only restores the real fetch. */
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('parked AuK surface', () => {
  it('renders no AuK load, generate, precision or intent chrome', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], calls))
    renderWorkbench()
    await findWorkbench()
    expect(screen.queryByText(/AuK/)).toBeNull()
    expect(screen.queryByRole('button', { name: /AuK/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Generate candidate' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'diagnostics' })).toBeNull()
    expect(screen.queryByLabelText(/^instruction$/i)).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    for (const intent of ['CLEAN', 'ISOLATE', 'PERFORM', 'EDIT', 'SYNTHESIZE']) {
      expect(screen.queryByRole('button', { name: intent })).toBeNull()
    }
    for (const task of ['enhance', 'denoise', 'repair', 'volume', 'clone', 'emotion']) {
      expect(screen.queryByRole('button', { name: task })).toBeNull()
    }
  })

  it('never calls a parked auk route', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice({ variants: [aukCandidate()] })], calls))
    renderWorkbench()
    await findWorkbench()
    await waitFor(() => {
      expect(calls.some((call) => call.url.includes('/api/runtime/e2'))).toBe(true)
    })
    expect(calls.filter((call) => call.url.includes('/auk'))).toEqual([])
  })
})

describe('workbench assets', () => {
  it('lists the profile sources, experiments and references with the ★ default', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [profileVoice()], calls))
    renderWorkbench()
    expect(await findWorkbench()).toBeInTheDocument()
    const sources = assetList('sources')
    expect(within(sources).getByText('take A')).toBeInTheDocument()
    expect(within(sources).getByText('take B')).toBeInTheDocument()
    const experiments = assetList('experiments')
    expect(within(experiments).getByText('enhance 1')).toBeInTheDocument()
    const references = assetList('references')
    expect(within(references).getByText('enhance 1 approved')).toBeInTheDocument()
    expect(within(references).getByText('★')).toBeInTheDocument()
    expect(within(experiments).queryByText('enhance 1 approved')).toBeNull()
  })

  it("derives one source and splits auk variants from today's 1:1 voice", async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch(
        () => [
          voice({
            variants: [
              aukCandidate(),
              aukCandidate({ id: 'rv_ok', audio_artifact_id: 'art_ok', approved: true }),
            ],
            active_reference_variant_id: 'rv_ok',
          }),
        ],
        calls,
      ),
    )
    renderWorkbench()
    await findWorkbench()
    expect(within(assetList('sources')).getByText('source')).toBeInTheDocument()
    expect(within(assetList('experiments')).getByText('enhance')).toBeInTheDocument()
    const references = assetList('references')
    expect(within(references).getByText('enhance')).toBeInTheDocument()
    expect(within(references).getByText('★')).toBeInTheDocument()
  })

  it('fails closed when the multi-source route is missing', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], calls))
    const { container } = renderWorkbench()
    await findWorkbench()
    fireEvent.change(screen.getByLabelText('add audio'), {
      target: { files: [new File(['riff'], 'take.wav', { type: 'audio/wav' })] },
    })
    expect(await screen.findByText(/multi-source audio is not available/i)).toBeInTheDocument()
    await waitFor(() => {
      expect(
        calls.some((call) => call.method === 'POST' && call.url.includes('/api/voices/vp1/sources')),
      ).toBe(true)
    })
    expect(audioSources(container).some((src) => src.includes('take'))).toBe(false)
  })

  it('parks speaker analysis out of the default surface even with a live analysis', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice({ speaker_analysis: ANALYSIS })], calls))
    renderWorkbench()
    await findWorkbench()
    expect(screen.queryByRole('button', { name: /Analyze speakers/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Speaker 1/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Use speaker/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /Play speaker/i })).toBeNull()
    expect(h.regions.filter((region) => String(region.id ?? '').startsWith('spk-'))).toEqual([])
    expect(calls.some((call) => call.url.includes('/speakers/'))).toBe(false)
  })

  it('parks the Resemble denoise cleanup out of the default surface', async () => {
    const profile = profileVoice()
    const denoised: VoiceArtifact = {
      id: 'a_den',
      role: 'experiment',
      kind: 'resemble',
      name: 'Resemble',
      audio_artifact_id: 'art_den',
      source_id: 'src_a',
      approved: false,
      default: false,
      stale: false,
    }
    vi.stubGlobal(
      'fetch',
      mockFetch(
        () => [
          {
            ...profile,
            artifacts: [...(profile.artifacts ?? []), denoised],
            variants: [DENOISE_VARIANT],
            active_variant: DENOISE_VARIANT,
            active_reference_variant_id: 'rv_den',
          },
        ],
        calls,
      ),
    )
    renderWorkbench()
    expect(await findWorkbench()).toBeInTheDocument()
    expect(screen.queryByText('Cleanup')).toBeNull()
    expect(screen.queryByRole('radio', { name: /denoise|cleanup/i })).toBeNull()
    expect(screen.queryByText(/^Original$/)).toBeNull()
    expect(screen.queryByText(/^Denoised/)).toBeNull()
    expect(screen.queryByText(/resemble/i)).toBeNull()
    const listed = [...assetList('experiments').querySelectorAll('button')].map(
      (node) => node.textContent,
    )
    expect(listed).toEqual(['enhance 1'])
  })

  it('shows no leftover chord, qwentts, cosyvoice or stream.fm chrome', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], calls))
    renderWorkbench()
    await findWorkbench()
    expect(screen.queryByText(/leftover[- ]parked/i)).toBeNull()
    expect(screen.queryByText(/not[- ]a[- ]pin[- ]swap/i)).toBeNull()
    expect(screen.queryByText(/qwentts/i)).toBeNull()
    expect(screen.queryByText(/cosyvoice/i)).toBeNull()
    expect(screen.queryByText(/stream\.fm/i)).toBeNull()
  })

  it('keeps the occupancy chrome on Breeze alone', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], calls))
    renderWorkbench()
    await findWorkbench()
    expect(screen.getByText(/GPU is free/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Load Breeze' })).toBeInTheDocument()
    expect(screen.queryByText(/AuK/)).toBeNull()
  })

  it('leaves a second source alone instead of cropping the primary one by accident', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [profileVoice()], calls))
    renderWorkbench()
    await findWorkbench()
    expect(screen.getByLabelText(/^transcript$/i)).toHaveValue('hello')
    fireEvent.click(within(assetList('sources')).getByRole('button', { name: 'take B' }))
    expect(screen.getByLabelText(/^transcript$/i)).toHaveValue('hello again')
    expect(screen.getByLabelText(/^transcript$/i)).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Keep only' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Exclude' })).toBeDisabled()
    fireEvent.click(within(assetList('sources')).getByRole('button', { name: 'take A' }))
    expect(screen.getByLabelText(/^transcript$/i)).toHaveValue('hello')
    expect(screen.getByLabelText(/^transcript$/i)).toBeEnabled()
    expect(screen.queryByText(/write the primary source/i)).toBeNull()
  })

  it('crops the source the voice columns describe, not the first one listed', async () => {
    // The backend folds the voice transcript into the primary source's row (`_sources_json`).
    const profile = profileVoice({ source_audio_artifact_id: 'art_src_b' })
    profile.sources = (profile.sources ?? []).map((item) =>
      item.id === 'src_b' ? { ...item, transcript: 'take B text' } : item,
    )
    vi.stubGlobal('fetch', mockFetch(() => [profile], calls))
    renderWorkbench()
    await findWorkbench()
    expect(screen.getByLabelText(/^transcript$/i)).toHaveValue('take B text')
    expect(screen.getByLabelText(/^transcript$/i)).toBeEnabled()
    fireEvent.click(within(assetList('sources')).getByRole('button', { name: 'take A' }))
    expect(screen.getByLabelText(/^transcript$/i)).toHaveValue('hello')
    expect(screen.getByLabelText(/^transcript$/i)).toBeDisabled()
  })
})

describe('workbench instrument', () => {
  it('keeps the crop controls, the transcript and the waveform instrument', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], calls))
    renderWorkbench()
    await findWorkbench()
    expect(screen.getByRole('button', { name: 'Keep only' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Exclude' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reset to original' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /play selection/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/^transcript$/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Transcribe from audio/ })).toBeInTheDocument()
  })

  it('paints the exclusion overlay for the gaps in the keep', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch(() => [voice({ keep_intervals: [{ start_s: 2, end_s: 6 }] })], calls),
    )
    renderWorkbench()
    await findWorkbench()
    await waitFor(() => {
      const exclusions = h.regions.filter((region) => String(region.id ?? '').startsWith('excl-'))
      expect(exclusions.map((region) => [region.start, region.end])).toEqual([
        [0, 2],
        [6, 12],
      ])
    })
  })

  it('loads Breeze without any parked processor in the way', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], calls))
    renderWorkbench()
    await findWorkbench()
    const load = screen.getByRole('button', { name: 'Load Breeze' })
    expect(load).toBeEnabled()
    fireEvent.click(load)
    await waitFor(() => {
      expect(calls.some((call) => call.url.includes('/api/runtime/e2/load'))).toBe(true)
    })
  })

  it('blocks Load Breeze while a live call holds the GPU', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch(() => [voice()], calls, { live_call_active: true, live_call_holder: 'hop' }),
    )
    renderWorkbench()
    await findWorkbench()
    const load = screen.getByRole('button', { name: 'Load Breeze' })
    expect(load).toBeDisabled()
    expect(screen.getByText(/live call is using the mouth/i)).toBeInTheDocument()
  })
})

describe('workbench compare', () => {
  it('auditions the source against the selected experiment and shows its lineage', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice({ variants: [aukCandidate()] })], calls))
    const { container } = renderWorkbench()
    await findWorkbench()
    expect(within(compareRegion()).getByText(/select an experiment/i)).toBeInTheDocument()
    fireEvent.click(within(assetList('experiments')).getByRole('button', { name: /enhance/ }))
    const sources = audioSources(container)
    expect(sources.some((src) => src.includes('/api/artifacts/art_src/audio'))).toBe(true)
    expect(sources.some((src) => src.includes('/api/artifacts/art_auk/audio'))).toBe(true)
    expect(within(inspector()).getByText(/seed 7/)).toBeInTheDocument()
    expect(within(inspector()).getByText(/Remove noise and reverberation/)).toBeInTheDocument()
  })

  it('drops an experiment from the compare strip without deleting it', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice({ variants: [aukCandidate()] })], calls))
    const { container } = renderWorkbench()
    await findWorkbench()
    fireEvent.click(within(assetList('experiments')).getByRole('button', { name: /enhance/ }))
    fireEvent.click(within(compareRegion()).getByRole('button', { name: /remove/i }))
    expect(audioSources(container).some((src) => src.includes('art_auk'))).toBe(false)
    expect(within(assetList('experiments')).getByText('enhance')).toBeInTheDocument()
  })

  it('keeps a stale experiment listed and marked', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch(() => [voice({ variants: [aukCandidate({ stale: true })] })], calls),
    )
    renderWorkbench()
    await findWorkbench()
    expect(within(assetList('experiments')).getByText(/stale/i)).toBeInTheDocument()
  })

  it('reports the quiet lineage of a profile reference', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [profileVoice()], calls))
    renderWorkbench()
    await findWorkbench()
    fireEvent.click(within(assetList('references')).getByRole('button', { name: /enhance 1 approved/ }))
    const lineage = inspector()
    expect(within(lineage).getByText('reference')).toBeInTheDocument()
    expect(within(lineage).getByText('a_exp')).toBeInTheDocument()
    expect(within(lineage).getByText('src_a')).toBeInTheDocument()
  })

  it('approves an experiment then activates it as the Breeze reference', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice({ variants: [aukCandidate()] })], calls))
    renderWorkbench()
    await findWorkbench()
    fireEvent.click(within(assetList('experiments')).getByRole('button', { name: /enhance/ }))
    fireEvent.click(within(compareRegion()).getByRole('button', { name: 'Approve' }))
    await waitFor(() => {
      const approve = calls.find((call) => call.url.includes('/auk/approve'))
      expect(approve?.body).toEqual({ variant_id: 'rv_auk' })
      const activate = calls.find((call) => call.url.includes('/reference/activate'))
      expect(activate?.body).toEqual({ variant_id: 'rv_auk' })
    })
  })
})

describe('workbench entry', () => {
  it('opens the workbench from the voices pane instead of an Edit reference overlay', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], calls))
    useWorkspace.setState({ selectedVoiceId: 'vp1', editorOpen: false })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <VoicesPane />
      </QueryClientProvider>,
    )
    const open = await screen.findByRole('button', { name: 'Open workbench' })
    expect(screen.queryByRole('button', { name: 'Edit reference' })).toBeNull()
    fireEvent.click(open)
    expect(useWorkspace.getState().editorOpen).toBe(true)
  })
})

describe('workbench in the lab shell', () => {
  it('fills the lab while the editor is open and closes on Done', async () => {
    vi.stubGlobal('fetch', mockFetch(() => [voice()], []))
    renderShell()
    expect(await findWorkbench()).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Voices' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: /Voice workbench/ })).toBeNull()
    })
    expect(screen.getByRole('heading', { name: 'Voices' })).toBeInTheDocument()
  })
})
