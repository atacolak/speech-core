import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Toaster } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SourceBench } from '@/features/sources/source-bench'
import type { MediaSource } from '@/features/sources/sources-api'
import type { Voice, VoiceArtifact, VoiceSource } from '@/lib/api'
import { useWorkspace } from '@/state/workspace'

type Call = { method: string; url: string; body: unknown }

/** The shapes the backend returns: source detail carries merged coverage, analyses and clips. */
function westworld(segments = DEFAULT_SEGMENTS): MediaSource {
  return {
    id: 'src_ww',
    kind: 'file',
    origin: 'ww-01.wav',
    title: 'Westworld S01E01',
    audio_artifact_id: 'art_src',
    waveform_artifact_id: 'art_wave',
    duration_s: 20,
    meta: {},
    created_at: '2026-09-14T00:00:00Z',
    coverage: [{ start_s: 0, end_s: 5 }],
    analyses: [
      {
        id: 'sa_1',
        start_s: 0,
        end_s: 5,
        processor: 'vibevoice',
        model_id: 'Dubedo/VibeVoice-ASR-HF-NF4',
        config: {},
        result: { segments },
        created_at: '2026-09-14T00:00:00Z',
      },
    ],
    speakers: [
      { local_id: 'S1', label: 'Speaker 1', duration_s: 2, mapped_voice_id: null },
      { local_id: 'S2', label: 'Speaker 2', duration_s: 3, mapped_voice_id: 'vp_ford' },
    ],
    clips: [],
  }
}

const DEFAULT_SEGMENTS = [
  { speaker_id: 'S1', start_s: 0, end_s: 2, text: 'one', overlap: false },
  { speaker_id: 'S2', start_s: 2, end_s: 5, text: 'two', overlap: false },
]

/** The source's enrolled rows: S2 is mapped onto Ford, whose primary source is this audio. */
const PRIMARY: VoiceSource = {
  id: 'vs_primary',
  label: 'Westworld S01E01',
  artifact_id: 'art_src',
  transcript: 'these violent delights have violent ends',
  transcript_locked: false,
  duration_s: 20,
  keep_intervals: [{ start_s: 0, end_s: 20 }],
}

const SECOND: VoiceSource = {
  id: 'vs_second',
  label: 'take B',
  artifact_id: 'art_src_b',
  transcript: 'take b text',
  transcript_locked: true,
  duration_s: 9,
  keep_intervals: [{ start_s: 0, end_s: 9 }],
}

const ORIGINAL: VoiceArtifact = {
  id: 'va_original',
  role: 'reference',
  kind: 'original',
  name: null,
  audio_artifact_id: 'art_src',
  parent_id: null,
  source_id: 'vs_primary',
  keep_intervals: [],
  approved: true,
  default: true,
  stale: false,
}

const CROP: VoiceArtifact = {
  id: 'va_crop',
  role: 'experiment',
  kind: 'crop',
  name: 'tight crop',
  audio_artifact_id: 'art_crop',
  parent_id: 'va_original',
  source_id: 'vs_primary',
  keep_intervals: [{ start_s: 0, end_s: 2 }],
  approved: false,
  default: false,
  stale: false,
}

function ford(overrides: Partial<Voice> = {}): Voice {
  return {
    id: 'vp_ford',
    name: 'Ford',
    tags: [],
    source_audio_artifact_id: 'art_src',
    original_artifact_id: 'art_src',
    original_format: 'wav',
    source_transcript: 'these violent delights have violent ends',
    keep_intervals: [{ start_s: 0, end_s: 20 }],
    effective_transcript: 'these violent delights have violent ends',
    active_reference_variant_id: null,
    variants: [],
    sources: [PRIMARY, SECOND],
    artifacts: [ORIGINAL],
    duration_s: 20,
    source_limit: 5,
    latest_take_id: null,
    created_at: '2026-09-14T00:00:00Z',
    updated_at: '2026-09-14T00:00:00Z',
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
 * The lab as the bench meets it: sources, voices, and the surgery routes that
 * write one enrolled source, cut a crop child and run Resemble on it. `capMessage`
 * makes extraction refuse the way a full voice does.
 */
function stubLab({
  status = 501,
  detail = 'not_implemented',
  voices = [ford()],
  capMessage = null,
}: {
  status?: number
  detail?: string
  voices?: Voice[]
  capMessage?: string | null
} = {}) {
  const calls: Call[] = []
  let listed = voices
  const fetchMock = vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const raw = init?.body ? String(init.body) : null
    calls.push({
      method,
      url,
      body: init?.body instanceof FormData ? Object.fromEntries(init.body.entries()) : raw ? (JSON.parse(raw) as unknown) : null,
    })
    if (url.includes('/api/runs')) {
      return json({ items: [] })
    }
    if (url.includes('/api/voices')) {
      const current = listed[0]
      if (method !== 'GET' && current) {
        if (url.includes('/crop')) {
          listed = [{ ...current, artifacts: [...(current.artifacts ?? []), CROP] }]
        } else if (url.includes('/denoise')) {
          listed = [
            {
              ...current,
              artifacts: [
                ...(current.artifacts ?? []),
                { ...CROP, id: 'va_denoise', kind: 'resemble', name: 'Resemble' },
              ],
            },
          ]
        } else if (method === 'PATCH') {
          const patch = JSON.parse(String(raw)) as { transcript?: string }
          listed = [
            {
              ...current,
              sources: (current.sources ?? []).map((item) =>
                item.id === 'vs_primary' && patch.transcript != null
                  ? { ...item, transcript: patch.transcript, transcript_locked: true }
                  : item,
              ),
            },
          ]
        }
        return json(listed[0])
      }
      return json({ items: listed })
    }
    if (url.includes('/extract') && capMessage !== null) {
      return json({ detail: { code: 'source_limit_reached', message: capMessage } }, 409)
    }
    if (url.includes('/analyze') || url.includes('/extract')) {
      if (status < 400) {
        return new Response(
          JSON.stringify(
            url.includes('/extract')
              ? {
                  id: 'clip_1',
                  source_id: 'src_ww',
                  speaker_local_id: 'S1',
                  voice_id: null,
                  ranges: [{ start_s: 0, end_s: 2 }],
                  segments: [],
                  audio_artifact_id: 'art_clip',
                  clean_transcript: 'one',
                  created_at: '2026-09-14T00:00:00Z',
                }
              : { ...westworld(), analyses_added: 1 },
          ),
          { status, headers: { 'Content-Type': 'application/json' } },
        )
      }
      return json({ detail }, status)
    }
    return json({ items: [] })
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

function renderBench(node: React.ReactElement = <SourceBench source={westworld()} />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      {node}
      <Toaster />
    </QueryClientProvider>,
  )
}

function setRange(from: string, to: string) {
  fireEvent.change(screen.getByLabelText('range from'), { target: { value: from } })
  fireEvent.change(screen.getByLabelText('range to'), { target: { value: to } })
}

/** The one POST/PATCH a bench action wrote, by the route it hit. */
function wrote(calls: Call[], path: string) {
  return calls.find((call) => call.method !== 'GET' && call.url.includes(path))
}

describe('source bench', () => {
  beforeEach(() => {
    useWorkspace.setState({ selectedVoiceId: null, selectedMaterialId: null })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows the waveform, coverage and source-local speaker lanes', () => {
    stubLab()
    renderBench()
    expect(screen.getByLabelText('waveform')).toBeInTheDocument()
    expect(screen.getByLabelText('analyzed coverage')).toHaveAttribute('data-coverage', '0.25')
    const lanes = screen.getByRole('list', { name: 'speaker lanes' })
    expect(lanes).toHaveTextContent('S1')
    expect(lanes).toHaveTextContent('S2')
    expect(lanes).toHaveTextContent('→ vp_ford')
  })

  it('does not analyze anything until the operator asks', async () => {
    const calls = stubLab()
    renderBench()
    expect(screen.getByRole('button', { name: 'ANALYZE selection' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'ANALYZE visible' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'ANALYZE whole' })).toBeEnabled()
    expect(calls.filter((call) => call.url.includes('/analyze'))).toEqual([])
  })

  it('keeps selection ANALYZE strictly on the picked range', async () => {
    const calls = stubLab()
    renderBench()
    const analyze = screen.getByRole('button', { name: 'ANALYZE selection' })
    expect(analyze).toBeDisabled()

    setRange('3', '2')
    expect(analyze).toBeDisabled()
    expect(screen.getByText(/range ends before it starts/i)).toBeInTheDocument()

    setRange('0', '25')
    expect(analyze).toBeDisabled()
    expect(screen.getByText(/range runs past the source/i)).toBeInTheDocument()

    setRange('4', '9')
    expect(analyze).toBeEnabled()
    expect(screen.getByTestId('selection-marker')).toBeInTheDocument()

    fireEvent.click(analyze)

    await waitFor(() => {
      expect(calls.some((call) => call.method === 'POST' && call.url.includes('/analyze'))).toBe(true)
    })
    expect(calls.find((call) => call.url.includes('/analyze'))?.body).toEqual({
      start_s: 4,
      end_s: 9,
    })
  })

  it('posts the whole-source range and fails closed without an analyzer', async () => {
    const calls = stubLab()
    renderBench()
    fireEvent.click(screen.getByRole('button', { name: 'ANALYZE whole' }))
    await waitFor(() => {
      expect(calls.some((call) => call.method === 'POST' && call.url.includes('/analyze'))).toBe(true)
    })
    expect(calls.find((call) => call.url.includes('/analyze'))?.body).toEqual({ all: true })
    expect((await screen.findAllByText(/source analysis is not available/i)).length).toBeGreaterThan(0)
  })

  it('analyzes the visible window when the whole source is in view', async () => {
    const calls = stubLab()
    renderBench()
    fireEvent.click(screen.getByRole('button', { name: 'ANALYZE visible' }))
    await waitFor(() => {
      expect(calls.some((call) => call.method === 'POST' && call.url.includes('/analyze'))).toBe(true)
    })
    expect(calls.find((call) => call.url.includes('/analyze'))?.body).toEqual({
      start_s: 0,
      end_s: 20,
    })
  })

  it('keeps extract disabled until one speaker has a turn selected', async () => {
    const calls = stubLab({ status: 404, detail: 'sources extract 404' })
    renderBench()
    const extract = screen.getByRole('button', { name: /^Extract/ })
    expect(extract).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: /S2 2\.00s/ }))
    expect(extract).toBeEnabled()
    fireEvent.click(extract)
    await waitFor(() => {
      expect(wrote(calls, '/extract')?.body).toEqual({
        speaker_local_id: 'S2',
        ranges: [{ start_s: 2, end_s: 5 }],
      })
    })
    expect(await screen.findByText(/clip extraction is not available/i)).toBeInTheDocument()
  })

  it('adds the extracted clip to the bench and refreshes the source', async () => {
    const calls = stubLab({ status: 200, detail: '' })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    render(
      <QueryClientProvider client={client}>
        <SourceBench source={westworld()} />
        <Toaster />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: /S1 0\.00s/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Extract/ }))

    expect(await screen.findByText(/clip 1 turn/i)).toBeInTheDocument()
    expect(calls.some((call) => call.url.includes('/extract'))).toBe(true)
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['sources'] })
  })

  it('excludes overlapping turns from a clip and refuses mixed speakers', () => {
    stubLab()
    const overlapping = [
      { speaker_id: 'S1', start_s: 0, end_s: 3, text: 'one', overlap: false },
      { speaker_id: 'S2', start_s: 2, end_s: 5, text: 'two', overlap: true },
      { speaker_id: 'S3', start_s: 6, end_s: 8, text: 'three', overlap: false },
    ]
    const source = westworld(overlapping)
    renderBench(
      <SourceBench
        source={{
          ...source,
          speakers: [
            ...source.speakers,
            { local_id: 'S3', label: 'Speaker 3', duration_s: 2, mapped_voice_id: null },
          ],
        }}
      />,
    )

    expect(screen.getByRole('button', { name: /S1 0\.00s/ })).toBeEnabled()
    expect(screen.getByRole('button', { name: /S2 2\.00s/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /S3 6\.00s/ })).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: /S1 0\.00s/ }))
    fireEvent.click(screen.getByRole('button', { name: /S3 6\.00s/ }))
    expect(screen.getByText(/one clip carries one speaker/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Extract/ })).toBeDisabled()
  })

  it('maps a source-local speaker onto a voice', async () => {
    const calls = stubLab()
    renderBench()
    const map = await screen.findByLabelText('Speaker 1 voice')
    expect(map).toHaveValue('')

    fireEvent.change(map, { target: { value: 'vp_ford' } })

    await waitFor(() => {
      expect(wrote(calls, '/speakers/S1/map')?.body).toEqual({ voice_id: 'vp_ford' })
    })
    expect(wrote(calls, '/speakers/S1/map')?.method).toBe('POST')
  })

  it('renders the source cap instead of enrolling at the limit', async () => {
    stubLab({ capMessage: 'voice source limit reached (1)' })
    renderBench()
    fireEvent.click(await screen.findByRole('button', { name: /S2 2\.00s/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Extract/ }))

    expect(await screen.findByText(/voice source limit reached \(1\)/i)).toBeInTheDocument()
  })

  it('writes one enrolled source transcript without touching its sibling', async () => {
    const calls = stubLab({ status: 200, detail: '' })
    renderBench()
    const editor = await screen.findByLabelText('source transcript')
    expect(editor).toHaveValue('these violent delights have violent ends')

    fireEvent.change(editor, { target: { value: 'ford says: these violent delights' } })
    fireEvent.blur(editor)

    await waitFor(() => {
      expect(wrote(calls, '/api/voices/vp_ford/sources/vs_primary')?.body).toEqual({
        transcript: 'ford says: these violent delights',
      })
    })
    expect(wrote(calls, '/api/voices/vp_ford/sources/vs_primary')?.method).toBe('PATCH')
    expect(wrote(calls, '/vs_second')).toBeUndefined()
  })

  it('edits the enrolled source the operator picked, not the first one listed', async () => {
    stubLab()
    renderBench()
    const picker = await screen.findByLabelText('Enrolled source')
    expect(await screen.findByLabelText('source transcript')).toHaveValue(
      'these violent delights have violent ends',
    )
    expect(screen.getByRole('button', { name: 'Resemble source' })).toBeEnabled()

    fireEvent.change(picker, { target: { value: 'vs_second' } })

    expect(screen.getByLabelText('source transcript')).toHaveValue('take b text')
    // Resemble is the primary source's operation; a sibling crop has no such home.
    expect(screen.getByRole('button', { name: 'Resemble source' })).toBeDisabled()
  })

  it('cuts a crop child beside its parent and leaves the parent in place', async () => {
    const calls = stubLab({ status: 200, detail: '' })
    renderBench()
    const crop = await screen.findByRole('button', { name: 'Create crop' })
    expect(crop).toBeDisabled()

    fireEvent.click(await screen.findByRole('button', { name: /S1 0\.00s/ }))
    fireEvent.change(screen.getByLabelText('Crop name'), { target: { value: 'tight crop' } })
    expect(crop).toBeEnabled()

    fireEvent.click(crop)

    await waitFor(() => {
      expect(wrote(calls, '/api/voices/vp_ford/sources/vs_primary/crop')?.body).toEqual({
        intervals: [{ start_s: 0, end_s: 2 }],
        name: 'tight crop',
      })
    })
    const lineage = await screen.findByRole('list', { name: 'source lineage' })
    expect(lineage).toHaveTextContent('original → tight crop')

    // The lineage link back to the voice tab is the whole point of the child row.
    fireEvent.click(screen.getByRole('button', { name: 'Show in voice store' }))
    expect(useWorkspace.getState().selectedVoiceId).toBe('vp_ford')
    expect(useWorkspace.getState().selectedMaterialId).toBeNull()
  })

  it('runs Resemble on the enrolled primary source', async () => {
    const calls = stubLab({ status: 200, detail: '' })
    renderBench()
    const resemble = await screen.findByRole('button', { name: 'Resemble source' })
    expect(resemble).toBeEnabled()

    fireEvent.click(resemble)

    await waitFor(() => {
      expect(wrote(calls, '/api/voices/vp_ford/sources/vs_primary/denoise')?.method).toBe('POST')
    })
  })

  it('renders no Say, Delivery or Generate inputs', async () => {
    stubLab()
    renderBench()
    await screen.findByLabelText('source transcript')
    for (const name of ['Say', 'Delivery', 'Generate', 'Save transcript']) {
      expect(screen.queryByText(new RegExp(`^${name}$`, 'i'))).toBeNull()
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.queryByLabelText('steer')).toBeNull()
  })
})
