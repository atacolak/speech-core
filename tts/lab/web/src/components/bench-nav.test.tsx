import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '@/app'
import { useWorkspace } from '@/state/workspace'

const RUNTIME = {
  selected: 'E2',
  status: 'unloaded',
  state: 'unloaded',
  leftover_parked: false,
  not_a_pin_swap: true,
  voicecat_path: true,
  engine: 'breeze-tts2',
  worker_pid: null,
  required_vram_bytes: 1,
  free_vram_bytes: 2,
  last_error: null,
}

/** Ford carries one approved reference: enough for a quiet voice bench, no empty panels. */
const VOICE = {
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
  variants: [],
  duration_s: 8,
  created_at: '2026-09-14T00:00:00Z',
  updated_at: '2026-09-14T00:00:00Z',
}

const MATERIAL = {
  id: 'src_ww',
  kind: 'file',
  origin: 'ww-01.wav',
  title: 'Westworld S01E01',
  audio_artifact_id: 'art_src',
  waveform_artifact_id: 'art_wave',
  duration_s: 20,
  meta: {},
  created_at: '2026-09-14T00:00:00Z',
  coverage: [],
  analyses: [],
  speakers: [],
  clips: [],
}

/** One take Ford's store lists under GENERATIONS. */
const TAKE = {
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

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function labFetch({
  voices = [VOICE],
  sources = [MATERIAL],
  runs = [],
}: {
  voices?: unknown[]
  sources?: unknown[]
  runs?: unknown[]
} = {}) {
  return vi.fn(async (input: RequestInfo) => {
    const url = String(input)
    if (url.includes('/api/runtime')) {
      return json(RUNTIME)
    }
    if (url.includes('/api/sources')) {
      return json({ items: sources })
    }
    if (url.includes('/api/voices')) {
      return json({ items: voices })
    }
    if (url.includes('/api/runs')) {
      return json({ items: runs })
    }
    if (url.endsWith('/api/fixtures/steers')) {
      return json({ items: [] })
    }
    if (url.includes('/api/conversations')) {
      return json({ items: [] })
    }
    return json({ detail: 'missing' }, 404)
  })
}

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  )
}

/** DESK | GENERATE | CONVERSATIONS | VOICE LAB is the whole top nav. */
describe('lab modes', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', labFetch())
    useWorkspace.setState({ mode: 'generate' })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    useWorkspace.setState({ mode: 'generate' })
    window.history.replaceState({}, '', '/')
  })

  it('swaps GENERATE for VOICE LAB and keeps Sources out of the nav', async () => {
    renderApp()
    const nav = screen.getByRole('navigation', { name: 'Lab modes' })
    const buttons = within(nav).getAllByRole('button')
    expect(buttons).toHaveLength(4)
    expect(buttons.map((button) => button.textContent)).toEqual([
      'DESK',
      'GENERATE',
      'CONVERSATIONS',
      'VOICE LAB',
    ])
    expect(within(nav).queryByRole('button', { name: /WORKBENCH|SOURCES/ })).toBeNull()
    const generate = screen.getByRole('button', { name: 'GENERATE' })
    const voiceLab = screen.getByRole('button', { name: 'VOICE LAB' })
    expect(generate).toHaveAttribute('aria-current', 'page')
    expect(screen.getByText(/^Say$/i)).toBeInTheDocument()

    fireEvent.click(voiceLab)

    expect(voiceLab).toHaveAttribute('aria-current', 'page')
    expect(generate).not.toHaveAttribute('aria-current')
    expect(screen.queryByRole('button', { name: 'Sources' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sources' })).toBeNull()
    expect(screen.queryByText(/^Say$/i)).toBeNull()
    expect(screen.getByRole('heading', { name: 'Voices' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Material' })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Ford/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Westworld S01E01/ })).toBeInTheDocument()
    expect(screen.getByText(/Pick a voice or some material/i)).toBeInTheDocument()

    fireEvent.click(generate)

    expect(generate).toHaveAttribute('aria-current', 'page')
    expect(screen.getByText(/^Say$/i)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Material' })).toBeNull()
  })

  it('swaps GENERATE for CONVERSATIONS and DESK', async () => {
    renderApp()
    expect(screen.getByRole('button', { name: 'GENERATE' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByText(/^Say$/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'CONVERSATIONS' }))

    expect(screen.getByRole('button', { name: 'CONVERSATIONS' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(screen.getByRole('heading', { name: 'CONVERSATIONS' })).toBeInTheDocument()
    expect(screen.queryByText(/^Say$/i)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'DESK' }))

    expect(screen.getByRole('button', { name: 'DESK' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { name: 'DESK' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'CONVERSATIONS' })).toBeNull()
  })

  it('says so when the library is empty', async () => {
    vi.stubGlobal('fetch', labFetch({ voices: [], sources: [] }))
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: 'VOICE LAB' }))
    expect(await screen.findByText(/No voices yet/i)).toBeInTheDocument()
    expect(screen.getByText(/No material yet/i)).toBeInTheDocument()
    expect(screen.getByText(/Pick a voice or some material/i)).toBeInTheDocument()
  })

  it('opens the voice store for a listed voice', async () => {
    vi.stubGlobal('fetch', labFetch({ runs: [TAKE] }))
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: 'VOICE LAB' }))
    fireEvent.click(await screen.findByRole('button', { name: /Ford/ }))

    const originals = await screen.findByRole('region', { name: 'Originals' })
    expect(originals).toHaveTextContent('8.00s')
    expect(await screen.findByRole('region', { name: 'Takes' })).toHaveTextContent('6.00s')
    // Not the retired piles, not the parked workbench skeleton: no permanent
    // inspector, no empty compare, no second workbench surface.
    for (const name of ['REFERENCES', 'SOURCE MATERIAL', 'DERIVATIVES', 'GENERATIONS']) {
      expect(screen.queryByRole('region', { name })).toBeNull()
    }
    expect(screen.queryByRole('complementary', { name: 'inspector' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'compare' })).toBeNull()
    expect(screen.queryByText(/Voice workbench/)).toBeNull()
  })

  it('opens the source bench waveform for a listed material', async () => {
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: 'VOICE LAB' }))
    fireEvent.click(await screen.findByRole('button', { name: /Westworld S01E01/ }))

    expect(await screen.findByLabelText('waveform')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Westworld S01E01' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'ANALYZE whole' })).toBeEnabled()
    expect(screen.queryByRole('region', { name: 'Originals' })).toBeNull()
  })

  it('keeps parked AuK chrome off both modes', async () => {
    renderApp()
    expect(await screen.findByRole('listitem', { name: 'Ford' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load AuK' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Unload AuK' })).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    for (const intent of ['CLEAN', 'ISOLATE', 'PERFORM', 'EDIT', 'SYNTHESIZE']) {
      expect(screen.queryByRole('button', { name: intent })).toBeNull()
    }

    fireEvent.click(screen.getByRole('button', { name: 'VOICE LAB' }))
    fireEvent.click(await screen.findByRole('button', { name: /Ford/ }))

    expect(await screen.findByRole('region', { name: 'Originals' })).toBeInTheDocument()
    expect(screen.queryByText(/AuK/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Load AuK' })).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    for (const intent of ['CLEAN', 'ISOLATE', 'PERFORM', 'EDIT', 'SYNTHESIZE']) {
      expect(screen.queryByRole('button', { name: intent })).toBeNull()
    }
  })
})
