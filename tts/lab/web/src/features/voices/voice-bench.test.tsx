import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { VoiceBench } from '@/features/voices/voice-bench'
import type { Voice, VoiceArtifact } from '@/lib/api'

/** The whole store in one profile: an enrolled source, its crop, and what the crop rendered. */
const VOICE: Voice = {
  id: 'vp_ford',
  name: 'Ford',
  tags: [],
  source_audio_artifact_id: 'art_src_a',
  original_artifact_id: 'art_ford_original',
  original_format: 'wav',
  source_transcript: 'these violent delights have violent ends',
  keep_intervals: [{ start_s: 0, end_s: 8 }],
  effective_transcript: 'these violent delights have violent ends',
  active_reference_variant_id: null,
  variants: [],
  sources: [
    {
      id: 'src_a',
      label: 'source take A',
      artifact_id: 'art_src_a',
      transcript: 'these violent delights have violent ends',
      duration_s: 12,
      keep_intervals: [{ start_s: 0, end_s: 8 }],
    },
    {
      id: 'src_b',
      label: 'source take B',
      artifact_id: 'art_src_b',
      transcript: 'hello again',
      duration_s: 9,
      keep_intervals: [{ start_s: 0, end_s: 9 }],
    },
  ],
  artifacts: [
    {
      id: 'a_ref',
      role: 'reference',
      kind: 'auk',
      name: 'enhance 1 approved',
      audio_artifact_id: 'art_ref',
      parent_id: 'a_crop',
      source_id: 'src_a',
      approved: true,
      default: true,
      stale: false,
    },
    {
      id: 'a_res',
      role: 'experiment',
      kind: 'resemble',
      name: 'Resemble denoise',
      audio_artifact_id: 'art_den',
      parent_id: 'a_crop',
      source_id: 'src_a',
      approved: false,
      default: false,
      stale: false,
    },
    {
      id: 'a_crop',
      role: 'experiment',
      kind: 'crop',
      name: 'Crop 0.0–8.0s',
      audio_artifact_id: 'art_crop',
      parent_id: 'a_orig_a',
      source_id: 'src_a',
      keep_intervals: [{ start_s: 0, end_s: 8 }],
      approved: false,
      default: false,
      stale: false,
    },
    {
      id: 'a_orig_a',
      role: 'reference',
      kind: 'original',
      name: 'Original',
      audio_artifact_id: 'art_src_a',
      parent_id: null,
      source_id: 'src_a',
      approved: true,
      default: false,
      stale: false,
    },
  ],
  default_reference_id: 'a_ref',
  duration_s: 12,
  latest_take_id: 'run_1',
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

function audioSources(scope: HTMLElement) {
  return Array.from(scope.querySelectorAll('audio')).map((node) => node.getAttribute('src') ?? '')
}

/** The `<li>` a labelled original lives in, so depth and order can be read off it. */
function rowFor(scope: HTMLElement, label: RegExp): HTMLElement {
  const row = within(scope).getByText(label).closest('li')
  if (!(row instanceof HTMLElement)) {
    throw new Error(`${label} is not in a row`)
  }
  return row
}

describe('voice bench', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('names ORIGINALS and GENERATIONS and none of the four old piles', async () => {
    stubLab([RUN])
    renderBench(VOICE)
    expect(await screen.findByRole('heading', { name: 'ORIGINALS' })).toBeInTheDocument()
    for (const old of ['REFERENCES', 'SOURCE MATERIAL', 'DERIVATIVES', 'TAKES']) {
      expect(screen.queryByRole('heading', { name: old })).toBeNull()
    }
    const originals = screen.getByRole('region', { name: 'ORIGINALS' })
    const generations = screen.getByRole('region', { name: 'GENERATIONS' })
    expect(await within(generations).findByText(/take run_1/i)).toBeInTheDocument()
    expect(within(originals).queryByText(/take run_1/i)).toBeNull()
  })

  it('nests each derived original under the parent its id names', async () => {
    stubLab([RUN])
    renderBench(VOICE)
    const originals = screen.getByRole('region', { name: 'ORIGINALS' })
    await within(screen.getByRole('region', { name: 'GENERATIONS' })).findByText(/take run_1/i)

    expect(rowFor(originals, /^source take A$/).dataset.depth).toBe('0')
    const crop = rowFor(originals, /^Crop 0\.0–8\.0s$/)
    const approved = rowFor(originals, /enhance 1 approved/)
    const denoised = rowFor(originals, /Resemble denoise/)
    expect(crop.dataset.depth).toBe('1')
    expect(approved.dataset.depth).toBe('2')
    expect(denoised.dataset.depth).toBe('2')

    // The payload lists the approved child first; the tree still follows parent ids.
    expect(crop.compareDocumentPosition(approved) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(crop).getByText('derived from source take A')).toBeInTheDocument()
    expect(within(approved).getByText('derived from Crop 0.0–8.0s')).toBeInTheDocument()
    expect(within(denoised).getByText('derived from Crop 0.0–8.0s')).toBeInTheDocument()
    // A root that derived from nothing carries no lineage text.
    expect(within(rowFor(originals, /^source take B$/)).queryByText(/derived from/i)).toBeNull()
    // An artifact the lab declares no length for shows no length at all.
    expect(within(originals).queryByText('—')).toBeNull()
  })

  it('marks the stored reference with the star and never a generation', async () => {
    stubLab([RUN])
    renderBench(VOICE)
    const originals = screen.getByRole('region', { name: 'ORIGINALS' })
    const generations = screen.getByRole('region', { name: 'GENERATIONS' })
    await within(generations).findByText(/take run_1/i)

    expect(within(originals).getAllByText('★')).toHaveLength(1)
    expect(within(rowFor(originals, /enhance 1 approved/)).getByText('★')).toBeInTheDocument()
    expect(within(generations).queryByText('★')).toBeNull()
  })

  it('surfaces the parent and its derived child side by side', async () => {
    stubLab([RUN])
    renderBench(VOICE)
    const originals = screen.getByRole('region', { name: 'ORIGINALS' })
    const generations = screen.getByRole('region', { name: 'GENERATIONS' })
    await within(generations).findByText(/take run_1/i)

    const sources = audioSources(originals)
    expect(sources.some((src) => src.includes('art_src_a'))).toBe(true)
    expect(sources.some((src) => src.includes('art_crop'))).toBe(true)
    expect(sources.some((src) => src.includes('art_ref'))).toBe(true)
    expect(audioSources(generations).some((src) => src.includes('art_take'))).toBe(true)
    expect(sources.some((src) => src.includes('art_take'))).toBe(false)
  })

  it('summarizes the source behind an original with its transcript', async () => {
    stubLab([RUN])
    renderBench(VOICE)
    const originals = screen.getByRole('region', { name: 'ORIGINALS' })
    await within(screen.getByRole('region', { name: 'GENERATIONS' })).findByText(/take run_1/i)

    expect(within(originals).getByText(/these violent delights have violent ends/)).toBeInTheDocument()
    expect(within(originals).getByText(/hello again/)).toBeInTheDocument()
  })

  it('stays quiet with nothing selected instead of a permanent compare panel', () => {
    stubLab()
    const { container } = renderBench(undefined)
    expect(screen.queryByText(/compare/i)).toBeNull()
    expect(screen.queryByRole('heading', { name: 'ORIGINALS' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'GENERATIONS' })).toBeNull()
    expect(container.firstChild).toBeNull()
  })

  it('never lets a derived row wear the star unless the lab stored it', async () => {
    stubLab([RUN])
    const artifacts = VOICE.artifacts as VoiceArtifact[]
    const stale: VoiceArtifact = {
      ...(artifacts.find((item) => item.id === 'a_ref') as VoiceArtifact),
      id: 'a_stale',
      name: 'stale render',
      audio_artifact_id: 'art_stale',
      default: false,
      stale: true,
    }
    renderBench({ ...VOICE, artifacts: [...artifacts, stale] })
    const originals = screen.getByRole('region', { name: 'ORIGINALS' })
    await screen.findByRole('heading', { name: 'GENERATIONS' })

    const row = rowFor(originals, /stale render/)
    expect(row.dataset.depth).toBe('2')
    expect(within(row).getByText(/· stale/)).toBeInTheDocument()
    expect(within(row).queryByText('★')).toBeNull()
    expect(within(originals).getAllByText('★')).toHaveLength(1)
  })

  it('carries no AuK chrome', async () => {
    stubLab([RUN])
    renderBench(VOICE)
    await within(screen.getByRole('region', { name: 'GENERATIONS' })).findByText(/take run_1/i)
    expect(screen.queryByRole('button', { name: /auk/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Load AuK' })).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    expect(screen.queryByText(/CLEAN|ISOLATE|PERFORM/)).toBeNull()
  })
})
