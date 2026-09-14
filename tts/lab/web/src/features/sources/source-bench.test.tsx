import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Toaster } from 'sonner'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SourceBench } from '@/features/sources/source-bench'
import type { MediaSource } from '@/features/sources/sources-api'

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

function stubLab({ status = 501, detail = 'not_implemented' } = {}) {
  const calls: Call[] = []
  const fetchMock = vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({
      method,
      url,
      body:
        init?.body instanceof FormData
          ? Object.fromEntries(init.body.entries())
          : init?.body
            ? (JSON.parse(String(init.body)) as unknown)
            : null,
    })
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
      return new Response(JSON.stringify({ detail }), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return new Response(JSON.stringify({ items: [] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
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

describe('source bench', () => {
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
      expect(calls.find((call) => call.url.includes('/extract'))?.body).toEqual({
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
})
