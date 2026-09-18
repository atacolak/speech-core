import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
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

const NAMED_RUN = {
  ...RUN,
  id: 'run_named',
  name: 'Evening take',
  output_artifact_id: 'art_named',
}

const UNTITLED_RUN = {
  ...RUN,
  id: 'run_untitled',
  name: null,
  output_artifact_id: 'art_untitled',
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

/** The `<li>` a labelled original lives in, so depth and order can be read off it. */
function rowFor(scope: HTMLElement, label: RegExp): HTMLElement {
  const row = within(scope).getByText(label).closest('li')
  if (!(row instanceof HTMLElement)) {
    throw new Error(`${label} is not in a row`)
  }
  return row
}

function openDerived(scope: HTMLElement, label: string) {
  fireEvent.click(within(scope).getByRole('button', { name: label }))
}

describe('voice bench', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders named take titles and an untitled placeholder in Takes, not Originals', async () => {
    stubLab([NAMED_RUN, UNTITLED_RUN])
    renderBench(VOICE)

    const originals = await screen.findByRole('region', { name: 'Originals' })
    const takes = screen.getByRole('region', { name: 'Takes' })
    const titles = await within(takes).findAllByLabelText('Take title')
    expect(titles.map((node) => (node as HTMLInputElement).value)).toEqual(['Evening take', ''])
    expect(titles.every((node) => node.getAttribute('placeholder') === 'Untitled')).toBe(true)
    expect(within(originals).queryByLabelText('Take title')).toBeNull()
    expect(within(originals).getByText('source take A')).toBeInTheDocument()
    expect(within(takes).queryByText('source take A')).toBeNull()
  })

  it('plays store rows without native audio controls', async () => {
    stubLab([NAMED_RUN])
    renderBench(VOICE)
    const originals = await screen.findByRole('region', { name: 'Originals' })
    const takes = screen.getByRole('region', { name: 'Takes' })
    await within(takes).findByDisplayValue('Evening take')
    expect(originals.querySelector('audio[controls]')).toBeNull()
    expect(takes.querySelector('audio[controls]')).toBeNull()
    expect(within(originals).getAllByRole('button', { name: 'Play' }).length).toBeGreaterThan(0)
    expect(within(takes).getAllByRole('button', { name: 'Play' }).length).toBeGreaterThan(0)
  })

  it('keeps derived originals collapsed until opened', async () => {
    stubLab([NAMED_RUN])
    renderBench(VOICE)
    const originals = await screen.findByRole('region', { name: 'Originals' })
    await screen.findByRole('region', { name: 'Takes' })

    expect(within(originals).getByText('source take A')).toBeInTheDocument()
    expect(within(originals).queryByText('Crop 0.0–8.0s')).toBeNull()
    expect(within(originals).queryByText('enhance 1 approved')).toBeNull()
    openDerived(originals, '1 derived')
    expect(within(originals).getByText('Crop 0.0–8.0s')).toBeInTheDocument()
    expect(within(originals).queryByText('enhance 1 approved')).toBeNull()
    openDerived(originals, '2 derived')
    expect(within(originals).getByText('enhance 1 approved')).toBeInTheDocument()
    expect(within(originals).getByText('Resemble denoise')).toBeInTheDocument()
  })

  it('names the canvas with the voice, not Voice store chrome', async () => {
    stubLab([NAMED_RUN])
    renderBench(VOICE)
    expect(await screen.findByRole('heading', { name: 'Ford' })).toBeInTheDocument()
    expect(screen.queryByText(/Voice store/)).toBeNull()
  })

  it('marks the stored reference with the star and never a take', async () => {
    stubLab([NAMED_RUN])
    renderBench(VOICE)
    const originals = await screen.findByRole('region', { name: 'Originals' })
    const takes = screen.getByRole('region', { name: 'Takes' })
    await within(takes).findByDisplayValue('Evening take')
    openDerived(originals, '1 derived')
    openDerived(originals, '2 derived')

    expect(within(originals).getAllByText('★')).toHaveLength(1)
    expect(within(rowFor(originals, /enhance 1 approved/)).getByText('★')).toBeInTheDocument()
    expect(within(takes).queryByText('★')).toBeNull()
  })

  it('hides transcripts until a row is expanded', async () => {
    stubLab([NAMED_RUN])
    renderBench(VOICE)
    const originals = await screen.findByRole('region', { name: 'Originals' })
    await screen.findByRole('region', { name: 'Takes' })

    expect(within(originals).queryByText(/these violent delights have violent ends/)).toBeNull()
    fireEvent.click(within(originals).getByText('source take A'))
    expect(within(originals).getByText(/these violent delights have violent ends/)).toBeInTheDocument()
  })

  it('stays quiet with nothing selected instead of a permanent compare panel', () => {
    stubLab()
    const { container } = renderBench(undefined)
    expect(screen.queryByText(/compare/i)).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Originals' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Takes' })).toBeNull()
    expect(container.firstChild).toBeNull()
  })

  it('never lets a derived row wear the star unless the lab stored it', async () => {
    stubLab([NAMED_RUN])
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
    const originals = await screen.findByRole('region', { name: 'Originals' })
    await screen.findByRole('region', { name: 'Takes' })
    openDerived(originals, '1 derived')
    openDerived(originals, '3 derived')

    const row = rowFor(originals, /stale render/)
    expect(row.dataset.depth).toBe('2')
    expect(within(row).getByText(/· stale/)).toBeInTheDocument()
    expect(within(row).queryByText('★')).toBeNull()
    expect(within(originals).getAllByText('★')).toHaveLength(1)
  })

  it('carries no AuK chrome', async () => {
    stubLab([NAMED_RUN])
    renderBench(VOICE)
    await within(screen.getByRole('region', { name: 'Takes' })).findByDisplayValue('Evening take')
    expect(screen.queryByRole('button', { name: /auk/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Load AuK' })).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    expect(screen.queryByText(/CLEAN|ISOLATE|PERFORM/)).toBeNull()
  })
})
