import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { InspectorPane } from '@/features/inspector/inspector-pane'
import { VoiceLab } from '@/features/voice-lab/voice-lab'
import { VoicesPane } from '@/features/voices/voices-pane'
import { useWorkspace } from '@/state/workspace'

type VoiceFixture = Record<string, unknown>

/** A profile voice: two enrolled references, one of them a clip with its own clean transcript. */
function profileVoice(overrides: VoiceFixture = {}): VoiceFixture {
  return {
    id: 'vp1',
    name: 'ata',
    tags: [],
    source_audio_artifact_id: 'art_src',
    original_artifact_id: 'art_src',
    original_format: 'wav',
    source_transcript: 'source A words',
    keep_intervals: [{ start_s: 0, end_s: 11.5 }],
    effective_transcript: 'source A words',
    sources: [
      {
        id: 'src_a',
        label: 'take A',
        artifact_id: 'art_src',
        transcript: 'source A words',
        duration_s: 11.5,
      },
    ],
    clips: [
      { id: 'clip_b', audio_artifact_id: 'art_clip_b', clean_transcript: 'clip B clean words' },
    ],
    artifacts: [
      {
        id: 'ref_a',
        role: 'reference',
        kind: 'original',
        name: 'take A',
        audio_artifact_id: 'art_src',
        source_id: 'src_a',
      },
      {
        id: 'ref_b',
        role: 'reference',
        kind: 'original',
        name: 'clip B',
        audio_artifact_id: 'art_clip_b',
        source_id: null,
      },
    ],
    default_reference_id: 'ref_a',
    active_reference_variant_id: null,
    active_variant: null,
    variants: [],
    speaker_analysis: null,
    duration_s: 11.5,
    source_duration_s: 12,
    effective_duration_s: 11.5,
    latest_take_id: 'run_1',
    created_at: '2026-09-16T00:00:00Z',
    updated_at: '2026-09-16T00:00:00Z',
    ...overrides,
  }
}

const ORIGINAL_VARIANT = {
  id: 'rv_orig',
  voice_profile_id: 'vp1',
  kind: 'original',
  audio_artifact_id: 'art_src',
  duration_s: 11.5,
}

/** A legacy voice: `reference_variants` rows are its references. */
function legacyVoice(
  activeVariant: Record<string, unknown> = {
    id: 'rv_den',
    voice_profile_id: 'vp1',
    kind: 'resemble',
    audio_artifact_id: 'art_den',
    duration_s: 11.5,
    stale: false,
  },
  overrides: VoiceFixture = {},
): VoiceFixture {
  return profileVoice({
    artifacts: [],
    default_reference_id: null,
    active_reference_variant_id: activeVariant.id,
    active_variant: activeVariant,
    variants: [ORIGINAL_VARIANT, activeVariant],
    ...overrides,
  })
}

/** A parked AuK candidate: its task word must never name the asset. */
const AUK_VARIANT = {
  id: 'rv_auk',
  voice_profile_id: 'vp1',
  kind: 'auk',
  audio_artifact_id: 'art_auk',
  duration_s: 11.5,
  auk_task: 'enhance',
  auk_precision: 'bf16',
  stale: false,
}

type Call = { url: string; method: string; body: string }

function stubLab(
  read: () => VoiceFixture,
  options: { runs?: unknown[]; activate?: (id: string) => void; voices?: () => VoiceFixture[] } = {},
) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo, init?: RequestInit) => {
      const url = String(input)
      const body = typeof init?.body === 'string' ? init.body : ''
      calls.push({ url, method: init?.method ?? 'GET', body })
      if (url.includes('/reference/activate')) {
        options.activate?.(JSON.parse(body).variant_id as string)
      }
      const payload = url.includes('/api/runtime')
        ? { active_voice_id: null, lease_owner: null, phase: 'idle', leases: [] }
        : url.includes('/api/runs')
          ? { items: options.runs ?? [] }
          : url.includes('/api/sources')
            ? { items: [] }
            : options.voices
              ? { items: options.voices() }
              : { items: [read()] }
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  return calls
}

function renderPane(node: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useWorkspace.setState({ selectedVoiceId: 'vp1' })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

function optionValues(picker: HTMLElement) {
  return Array.from(picker.querySelectorAll('option')).map((node) => node.value)
}

describe('reference picker chrome', () => {
  afterEach(() => {
    // The stub stays installed: panes refetch during teardown, and a relative URL
    // reaching the real fetch would throw after the run.
    useWorkspace.setState({ selectedVoiceId: null })
  })

  it('activates the picked reference and moves the quote and audio to its own origin', async () => {
    let voice = profileVoice()
    const calls = stubLab(() => voice, {
      activate: (id) => {
        voice = profileVoice({ default_reference_id: id })
      },
    })
    const { container } = renderPane(<VoicesPane />)

    const picker = await screen.findByLabelText('Reference')
    const card = picker.closest('div') as HTMLElement
    expect(within(picker).getByRole('option', { name: '★ take A' })).toBeInTheDocument()
    expect(await within(card).findByText('source A words')).toBeInTheDocument()

    fireEvent.change(picker, { target: { value: 'ref_b' } })

    expect(await within(card).findByText('clip B clean words')).toBeInTheDocument()
    const post = calls.find((call) => call.url.includes('/reference/activate'))
    expect(post?.method).toBe('POST')
    expect(post?.url).toContain('/api/voices/vp1/reference/activate')
    expect(JSON.parse(post?.body ?? '{}')).toEqual({ variant_id: 'ref_b' })
    expect(within(card).queryByText('source A words')).toBeNull()
    expect((picker as HTMLSelectElement).value).toBe('ref_b')
    expect(
      Array.from(card.querySelectorAll('audio')).some((node) =>
        (node.getAttribute('src') ?? '').includes('art_clip_b'),
      ),
    ).toBe(true)
    expect(container.querySelectorAll('audio').length).toBeGreaterThan(0)
  })

  it('offers the voice references in stored order and never a run', async () => {
    stubLab(() => profileVoice())
    renderPane(<VoicesPane />)

    const picker = await screen.findByLabelText('Reference')

    expect(optionValues(picker)).toEqual(['ref_a', 'ref_b'])
    expect(optionValues(picker)).not.toContain('run_1')
    expect(within(picker).getByRole('option', { name: 'clip B' })).toBeInTheDocument()
  })

  it('names a parked denoise variant without the resemble product name and plays its artifact', async () => {
    stubLab(() => legacyVoice())
    const { container } = renderPane(<VoicesPane />)

    const picker = await screen.findByLabelText('Reference')

    expect(within(picker).getByRole('option', { name: '★ denoised' })).toBeInTheDocument()
    expect(within(picker).getByRole('option', { name: 'original' })).toBeInTheDocument()
    expect(screen.queryByText(/resemble/i)).toBeNull()
    expect(
      Array.from(container.querySelectorAll('audio')).some((node) =>
        (node.getAttribute('src') ?? '').includes('art_den'),
      ),
    ).toBe(true)
  })

  it('flags a stale denoise variant and falls back to the keep crop', async () => {
    stubLab(() =>
      legacyVoice({
        id: 'rv_den',
        voice_profile_id: 'vp1',
        kind: 'resemble',
        audio_artifact_id: 'art_den',
        duration_s: 11.5,
        stale: true,
      }),
    )
    const { container } = renderPane(<VoicesPane />)

    const picker = await screen.findByLabelText('Reference')

    expect(within(picker).getByRole('option', { name: '★ denoised (stale)' })).toBeInTheDocument()
    const sources = Array.from(container.querySelectorAll('audio')).map(
      (node) => node.getAttribute('src') ?? '',
    )
    expect(sources.some((src) => src.includes('art_den'))).toBe(false)
    expect(sources.some((src) => src.includes('/reference/audio'))).toBe(true)
  })

  it('names a parked auk variant by a neutral word, not its task or the product', async () => {
    stubLab(() => legacyVoice(AUK_VARIANT))
    renderPane(<VoicesPane />)

    const picker = await screen.findByLabelText('Reference')

    expect(within(picker).getByRole('option', { name: '★ candidate' })).toBeInTheDocument()
    // The selected GENERATE row is the identity card, so the neutral word is read
    // off the picker option above, never a compact row subtitle.
    // Exact text only: prose may describe processing, but no asset is *named* by a task word.
    expect(screen.queryByText('enhance')).toBeNull()
    expect(screen.queryByText(/auk/i)).toBeNull()
  })

  it('reports the active reference kind in the settings pane', async () => {
    stubLab(() => legacyVoice())
    renderPane(<InspectorPane />)

    expect(await screen.findByText(/^denoised · /)).toBeInTheDocument()
    expect(screen.queryByText(/resemble/i)).toBeNull()
  })

  it('reports the parked auk variant as a candidate in the settings pane', async () => {
    stubLab(() => legacyVoice(AUK_VARIANT))
    renderPane(<InspectorPane />)

    expect(await screen.findByText(/^candidate · /)).toBeInTheDocument()
    expect(screen.queryByText(/auk/i)).toBeNull()
  })

  it('drops Takes and every voice bench pile from the inspector', async () => {
    stubLab(() => legacyVoice(), {
      runs: [
        {
          id: 'run_1',
          voice_id: 'vp1',
          output_artifact_id: 'art_take',
          latency_ms: 200,
          first_audio_ms: 30,
          duration_s: 3.2,
          rating: null,
          tags: [],
          request_snapshot: {
            text: 'Born. UNBORN.',
            produced_text: 'Born.',
            segments_planned: 2,
            segments_completed: 1,
            stopped: true,
            generation: { seed: 7 },
          },
        },
      ],
    })
    renderPane(<InspectorPane />)

    expect(await screen.findByText(/^denoised · /)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Takes' })).toBeNull()
    expect(screen.queryByText(/^cap$/i)).toBeNull()
    for (const name of ['REFERENCES', 'SOURCE MATERIAL', 'DERIVATIVES']) {
      expect(screen.queryByRole('heading', { name })).toBeNull()
      expect(screen.queryByRole('region', { name })).toBeNull()
    }
  })

  it('renders neither GENERATE workbench door', async () => {
    stubLab(() => profileVoice())
    renderPane(
      <>
        <VoicesPane />
        <InspectorPane />
      </>,
    )

    await screen.findByLabelText('Reference')

    expect(screen.queryByRole('button', { name: 'Open workbench' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  })

  it('expands the selected GENERATE voice in its list row', async () => {
    stubLab(() => profileVoice())
    renderPane(<VoicesPane />)

    const list = await screen.findByRole('list')
    const selected = within(list).getByRole('listitem')
    expect(selected).toHaveAccessibleName('ata')
    expect(within(selected).getByLabelText('Name')).toBeInTheDocument()
    expect(within(selected).getByLabelText('Reference')).toBeInTheDocument()
    expect(within(selected).getByRole('button', { name: 'Delete voice' })).toBeInTheDocument()
    const after = list.nextElementSibling
    expect(after?.querySelector?.('input, select')).toBeFalsy()
  })

  it('moves the expanded card to another voice when it is picked', async () => {
    stubLab(() => profileVoice(), {
      voices: () => [profileVoice(), profileVoice({ id: 'vp2', name: 'bee' })],
    })
    renderPane(<VoicesPane />)

    const list = await screen.findByRole('list')
    const [first, second] = within(list).getAllByRole('listitem')
    expect(within(first).getByLabelText('Name')).toBeInTheDocument()
    expect(within(second).queryByLabelText('Name')).toBeNull()

    fireEvent.click(within(second).getByRole('button', { name: /bee/ }))

    expect(await within(second).findByLabelText('Name')).toBeInTheDocument()
    expect(within(first).queryByLabelText('Name')).toBeNull()
  })

  it('keeps the Voice Lab library rows compact', async () => {
    stubLab(() => profileVoice())
    renderPane(<VoiceLab />)

    const row = (await screen.findByRole('button', { name: /ata/ })).closest('li') as HTMLElement
    expect(row.tagName).toBe('LI')
    const list = row.closest('ul') as HTMLElement
    expect(list.tagName).toBe('UL')
    for (const item of within(list).getAllByRole('listitem')) {
      expect(within(item).queryByLabelText('Name')).toBeNull()
    }
    expect(within(list).queryByRole('button', { name: 'Delete voice' })).toBeNull()
  })
})
