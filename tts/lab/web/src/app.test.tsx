import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '@/app'

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

const SAMPLE_VOICE = {
  id: 'vp_sample_george_hotz',
  name: 'George Hotz',
  tags: ['sample', 'dogfood'],
  source_audio_artifact_id: 'art_sample',
  original_artifact_id: 'art_sample_orig',
  original_format: 'mp3',
  source_transcript: '',
  keep_intervals: [{ start_s: 0, end_s: 12 }],
  effective_transcript: '',
  active_reference_variant_id: 'rv_sample_george_hotz',
  active_variant: {
    id: 'rv_sample_george_hotz',
    voice_profile_id: 'vp_sample_george_hotz',
    kind: 'original',
    audio_artifact_id: 'art_sample',
    duration_s: 12,
  },
  variants: [],
  duration_s: 12,
  created_at: '2026-09-09T00:00:00Z',
  updated_at: '2026-09-09T00:00:00Z',
}

const OTHER_VOICE = {
  ...SAMPLE_VOICE,
  id: 'vp_ata',
  name: 'ata',
  tags: [],
  source_audio_artifact_id: 'art_ata',
  original_artifact_id: 'art_ata',
  original_format: 'wav',
  active_reference_variant_id: 'rv_ata',
  active_variant: {
    id: 'rv_ata',
    voice_profile_id: 'vp_ata',
    kind: 'original',
    audio_artifact_id: 'art_ata',
    duration_s: 3,
  },
  duration_s: 3,
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockLabFetch(voices: unknown[] = []) {
  return vi.fn(async (input: RequestInfo) => {
    const url = String(input)
    if (url.includes('/api/runtime')) {
      return jsonResponse(RUNTIME)
    }
    if (url.includes('/api/voices')) {
      return jsonResponse({ items: voices })
    }
    if (url.includes('/api/runs')) {
      return jsonResponse({ items: [] })
    }
    if (url.includes('/api/fixtures/steers')) {
      return jsonResponse({ items: [] })
    }
    if (url.includes('/api/artifacts/') && url.includes('/audio')) {
      return new Response(new Uint8Array([82, 73, 70, 70]), { status: 200 })
    }
    return jsonResponse({ detail: 'missing' }, 404)
  })
}

function renderApp(ui: ReactElement = <App />) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

describe('TTS lab shell', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', mockLabFetch())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('opens on GENERATE with the voice picker and composition, settings on demand', async () => {
    renderApp()
    expect(screen.getByRole('banner')).toHaveTextContent('TTS lab')
    expect(screen.getByRole('button', { name: 'GENERATE' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { name: 'Voices' })).toBeInTheDocument()
    expect(screen.getByText(/^Say$/i)).toBeInTheDocument()
    expect(screen.getByText(/^Delivery$/i)).toBeInTheDocument()
    expect(await screen.findByText(/No voices yet/i)).toBeInTheDocument()
    expect(screen.getAllByText(/No takes yet/i).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Generate' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '+ Import voice' })).toBeInTheDocument()
    // Settings is a drawer, not a permanently allocated panel.
    expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull()
    expect(screen.queryByText(/cfg_scale/i)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    expect(await screen.findByRole('heading', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Takes' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save transcript' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use selection' })).not.toBeInTheDocument()
  })

  it('auto-selects the George Hotz sample as the dogfood voice', async () => {
    vi.stubGlobal('fetch', mockLabFetch([OTHER_VOICE, SAMPLE_VOICE]))
    renderApp()
    expect(await screen.findByRole('button', { name: /George Hotz/i })).toBeInTheDocument()
    expect(screen.getAllByText(/sample/i).length).toBeGreaterThan(0)
    expect(screen.getByText(/Voice: George Hotz · sample/i)).toBeInTheDocument()
    expect(screen.queryByText(/No voices yet/i)).not.toBeInTheDocument()
  })
})

describe('parked AuK surface', () => {
  beforeEach(() => {
    // jsdom has no matchMedia; WaveSurfer's region drag asks for it when the workbench mounts.
    vi.stubGlobal(
      'matchMedia',
      (query: string): MediaQueryList =>
        ({
          matches: false,
          media: query,
          onchange: null,
          addEventListener: () => {},
          removeEventListener: () => {},
          addListener: () => {},
          removeListener: () => {},
          dispatchEvent: () => false,
        }) as unknown as MediaQueryList,
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('opens the workbench with no AuK chrome and no auk traffic', async () => {
    const fetchMock = mockLabFetch([SAMPLE_VOICE])
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    fireEvent.click(await screen.findByRole('button', { name: 'Open workbench' }))
    expect(await screen.findByRole('heading', { name: /Voice workbench/ })).toBeInTheDocument()
    // Positive control: this is the load control that sat beside Load/Unload AuK.
    expect(screen.getByRole('button', { name: 'Load Breeze' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load AuK' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Unload AuK' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Generate candidate' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'diagnostics' })).toBeNull()
    expect(screen.queryByText(/AuK/)).toBeNull()
    for (const intent of ['CLEAN', 'ISOLATE', 'PERFORM', 'EDIT', 'SYNTHESIZE']) {
      expect(screen.queryByRole('button', { name: intent })).toBeNull()
    }
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    expect(screen.queryByText(/qwentts|cosyvoice|Analyze speakers/i)).toBeNull()
    // The sample voice carries no experiments, so any task chip here would be the parked cookbook.
    for (const task of ['enhance', 'denoise', 'repair', 'volume', 'clone', 'emotion']) {
      expect(screen.queryByRole('button', { name: task })).toBeNull()
    }
    const auk = fetchMock.mock.calls.filter(([input]) => String(input).includes('/auk'))
    expect(auk).toEqual([])
  })
})

describe('runtime indicator', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', mockLabFetch())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows Breeze TTS2 unloaded with a Load control', async () => {
    renderApp()
    const chip = await screen.findByRole('button', { name: /Breeze TTS2 unloaded/i })
    expect(chip).toBeEnabled()
    expect(chip).toHaveAttribute('data-status', 'unloaded')
    expect(screen.getByTestId('runtime-status')).toHaveAttribute('data-status', 'unloaded')
    expect(screen.queryByText(/leftover parked/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/not a pin swap/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Loading E2/i)).not.toBeInTheDocument()
  })

  it('shows an error state when runtime fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network')))
    renderApp()
    await waitFor(() => {
      expect(screen.getByTestId('runtime-status')).toHaveAttribute('data-status', 'error')
    })
    expect(screen.getByRole('banner')).toHaveTextContent('TTS lab')
    expect(screen.getByRole('button', { name: /Breeze TTS2 error/i })).toBeInTheDocument()
  })
})
