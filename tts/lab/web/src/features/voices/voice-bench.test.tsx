import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { VoiceBench } from '@/features/voices/voice-bench'
import type { Voice } from '@/lib/api'

const VOICE: Voice = {
  id: 'vp_ford',
  name: 'Ford',
  tags: [],
  source_audio_artifact_id: 'art_ford_ref',
  original_artifact_id: 'art_ford_src',
  original_format: 'wav',
  source_transcript: '',
  keep_intervals: [{ start_s: 0, end_s: 8 }],
  effective_transcript: 'these violent delights have violent ends',
  active_reference_variant_id: 'rv_ford',
  active_variant: {
    id: 'rv_ford',
    voice_profile_id: 'vp_ford',
    kind: 'original',
    audio_artifact_id: 'art_ford_ref',
    duration_s: 8,
  },
  variants: [
    {
      id: 'rv_den',
      voice_profile_id: 'vp_ford',
      kind: 'resemble',
      audio_artifact_id: 'art_ford_den',
      duration_s: 8,
      stale: false,
    },
    {
      id: 'rv_enh',
      voice_profile_id: 'vp_ford',
      kind: 'resemble',
      audio_artifact_id: 'art_ford_enh',
      duration_s: 8,
      stale: false,
    },
  ],
  sources: [
    { id: 'vs_1', label: 'westworld_clip_01', artifact_id: 'art_clip_01', duration_s: 8 },
    { id: 'vs_2', label: 'westworld_clip_02', artifact_id: 'art_clip_02', duration_s: 4 },
  ],
  duration_s: 8,
  created_at: '2026-09-14T00:00:00Z',
  updated_at: '2026-09-14T00:00:00Z',
}

const RUN = {
  id: 'run_1',
  voice_id: 'vp_ford',
  output_artifact_id: 'art_take',
  latency_ms: 900,
  first_audio_ms: 120,
  duration_s: 6,
  rating: null,
  tags: [],
  created_at: '2026-09-14T00:00:00Z',
}

function stubLab(runs: unknown[] = []) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = String(input)
      if (url.includes('/api/runs')) {
        return new Response(JSON.stringify({ items: runs }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ detail: 'missing' }), { status: 404 })
    }),
  )
}

function renderBench(voice: Voice | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <VoiceBench voice={voice} />
    </QueryClientProvider>,
  )
}

/** The backend adds `clips` to the voice profile; the shared Voice type has not caught up. */
const CLIPS = [
  {
    id: 'clip_1',
    source_id: 'src_ww',
    speaker_local_id: 'S1',
    voice_id: 'vp_ford',
    ranges: [{ start_s: 2, end_s: 5 }],
    segments: [],
    audio_artifact_id: 'art_clip_a',
    clean_transcript: 'these violent delights',
    created_at: '2026-09-14T00:00:00Z',
    source_title: 'Westworld S01E01',
    source_kind: 'file' as const,
  },
]

const VOICE_WITH_CLIPS = { ...VOICE, clips: CLIPS } as Voice

function audioSources(container: HTMLElement) {
  return Array.from(container.querySelectorAll('audio')).map((node) => node.getAttribute('src') ?? '')
}

describe('voice bench', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('names the four voice sections for the selected voice', async () => {
    stubLab([RUN])
    renderBench(VOICE)
    expect(await screen.findByRole('heading', { name: 'TAKES' })).toBeInTheDocument()
    for (const heading of ['REFERENCES', 'SOURCE MATERIAL', 'DERIVATIVES', 'TAKES']) {
      expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    }
    expect(within(screen.getByRole('region', { name: 'TAKES' })).getByText(/take run_1/i)).toBeInTheDocument()
  })


  it('lists the clips that fed the voice, not the original media', () => {
    stubLab()
    renderBench(VOICE)
    const material = screen.getByRole('region', { name: 'SOURCE MATERIAL' })
    expect(within(material).getByText('westworld_clip_01')).toBeInTheDocument()
    expect(within(material).getByText('westworld_clip_02')).toBeInTheDocument()
  })

  it('prefers the clips the backend attaches to the profile', () => {
    stubLab()
    renderBench(VOICE_WITH_CLIPS)
    const material = screen.getByRole('region', { name: 'SOURCE MATERIAL' })
    expect(within(material).getByText('Westworld S01E01')).toBeInTheDocument()
    expect(within(material).getByText('3.00s')).toBeInTheDocument()
    // The profile's own sources are the fallback, not a second list.
    expect(within(material).queryByText('westworld_clip_01')).toBeNull()
  })

  it('plays the crop, not a stale derivative, as the reference', () => {
    stubLab()
    renderBench({
      ...VOICE,
      active_reference_variant_id: 'rv_den',
      active_variant: {
        id: 'rv_den',
        voice_profile_id: 'vp_ford',
        kind: 'resemble',
        audio_artifact_id: 'art_ford_den',
        duration_s: 8,
        stale: true,
      },
    })
    const references = screen.getByRole('region', { name: 'REFERENCES' })
    expect(within(references).getByText(/denoised/)).toBeInTheDocument()
    expect(within(references).getByText(/stale/)).toBeInTheDocument()
    const sources = audioSources(references)
    expect(sources.some((src) => src.includes('art_ford_den'))).toBe(false)
    expect(sources.some((src) => src.includes('art_ford_ref'))).toBe(true)
  })

  it('never invents a length for an artifact the backend leaves open', () => {
    stubLab()
    renderBench({
      ...VOICE,
      artifacts: [
        {
          id: 'va_ref',
          role: 'reference',
          kind: 'resemble',
          name: 'Resemble take',
          audio_artifact_id: 'art_va_ref',
        },
      ],
    })
    const references = screen.getByRole('region', { name: 'REFERENCES' })
    expect(within(references).getByText(/Resemble take/)).toBeInTheDocument()
    expect(within(references).queryByText(/—/)).toBeNull()
    // The profile's active variant still knows its length.
    expect(within(references).queryByText(/denoised/)).toBeNull()
  })

  it('shows the approved reference and its derivatives', () => {
    stubLab()
    renderBench(VOICE)
    const references = screen.getByRole('region', { name: 'REFERENCES' })
    expect(within(references).getByText(/original/)).toBeInTheDocument()
    const derivatives = screen.getByRole('region', { name: 'DERIVATIVES' })
    expect(within(derivatives).getAllByText(/denoised/)).toHaveLength(2)
  })

  it('stays quiet with nothing selected instead of a permanent compare panel', () => {
    stubLab()
    const { container } = renderBench(undefined)
    expect(screen.queryByRole('region', { name: 'compare' })).toBeNull()
    expect(screen.queryByText(/compare/i)).toBeNull()
    expect(screen.queryByRole('heading', { name: 'REFERENCES' })).toBeNull()
    expect(container.firstChild).toBeNull()
  })


  it('carries no AuK chrome', () => {
    stubLab()
    renderBench(VOICE)
    expect(screen.queryByRole('button', { name: /auk/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Load AuK' })).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    expect(screen.queryByText(/CLEAN|ISOLATE|PERFORM/)).toBeNull()
  })
})
