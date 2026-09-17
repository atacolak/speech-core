/**
 * The workbench capability ledger, driven through the lab shell.
 *
 * Every capability the deleted `features/voices/workbench.tsx` owned is asserted
 * at its new home: the Voice Lab's existing MaterialLibrary -> SourceBench, or
 * the selected voice's store. The old surface is never imported here.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { Toaster } from 'sonner'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppShell } from '@/components/app-shell'
import type { Voice, VoiceArtifact, VoiceSource } from '@/lib/api'
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
  live_call_active: false,
  processor: null,
}

/** The material the operator works: two source-local speakers, one already mapped to Ford. */
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
  coverage: [{ start_s: 0, end_s: 5 }],
  analyses: [
    {
      id: 'sa_1',
      start_s: 0,
      end_s: 5,
      processor: 'vibevoice',
      model_id: 'Dubedo/VibeVoice-ASR-HF-NF4',
      config: {},
      result: {
        segments: [{ speaker_id: 'S1', start_s: 0, end_s: 2, text: 'one', overlap: false }],
      },
      created_at: '2026-09-14T00:00:00Z',
    },
  ],
  speakers: [
    { local_id: 'S1', label: 'Speaker 1', duration_s: 2, mapped_voice_id: null },
    { local_id: 'S2', label: 'Speaker 2', duration_s: 3, mapped_voice_id: 'vp_ford' },
  ],
  clips: [],
}

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

/** Ford's store: the enrolled material, a sibling take, and the crop cut out of the first. */
function ford(artifacts: VoiceArtifact[] = [ORIGINAL]): Voice {
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
    artifacts,
    default_reference_id: 'va_original',
    duration_s: 20,
    source_limit: 5,
    latest_take_id: null,
    created_at: '2026-09-14T00:00:00Z',
    updated_at: '2026-09-14T00:00:00Z',
  }
}

type Call = { method: string; url: string; body: unknown }

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockLab(initial: Voice = ford()) {
  const calls: Call[] = []
  let voice = initial
  const fetchMock = vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const raw = init?.body ? String(init.body) : null
    calls.push({
      method,
      url,
      body: init?.body instanceof FormData ? Object.fromEntries(init.body.entries()) : raw ? (JSON.parse(raw) as unknown) : null,
    })
    if (url.includes('/api/runtime')) {
      return json(RUNTIME)
    }
    if (url.includes('/api/voices')) {
      if (method === 'POST' && url.includes('/crop')) {
        voice = { ...voice, artifacts: [...(voice.artifacts ?? []), CROP] }
        return json(voice)
      }
      if (method === 'POST' && url.includes('/denoise')) {
        voice = {
          ...voice,
          artifacts: [
            ...(voice.artifacts ?? []),
            { ...CROP, id: 'va_denoise', kind: 'resemble', name: 'Resemble' },
          ],
        }
        return json(voice)
      }
      if (method === 'PATCH') {
        const patch = JSON.parse(String(raw)) as { transcript?: string }
        voice = {
          ...voice,
          sources: (voice.sources ?? []).map((item) =>
            item.id === 'vs_primary' && patch.transcript != null
              ? { ...item, transcript: patch.transcript, transcript_locked: true }
              : item,
          ),
        }
        return json(voice)
      }
      return json({ items: [voice] })
    }
    if (url.includes('/api/sources') && method === 'POST') {
      return json(MATERIAL)
    }
    if (url.includes('/api/sources')) {
      return json({ items: [MATERIAL] })
    }
    if (url.includes('/api/runs')) {
      return json({ items: [] })
    }
    if (url.endsWith('/api/fixtures/steers')) {
      return json({ items: [] })
    }
    return json({ detail: 'missing' }, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

function renderShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AppShell />
      <Toaster />
    </QueryClientProvider>,
  )
}

/** GENERATE | VOICE LAB is the whole shell; the workbench lives inside the lab. */
async function openMaterial() {
  fireEvent.click(screen.getByRole('button', { name: 'VOICE LAB' }))
  fireEvent.click(await screen.findByRole('button', { name: /Westworld S01E01/ }))
  await screen.findByLabelText('waveform')
}

describe('workbench capability ledger in the lab shell', () => {
  beforeEach(() => {
    useWorkspace.setState({
      mode: 'generate',
      selectedVoiceId: null,
      selectedMaterialId: null,
      selectedRunId: null,
      settingsOpen: false,
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('is entered by choosing material inside Voice Lab, and by nothing else', async () => {
    mockLab()
    renderShell()

    // The GENERATE doors that used to open a parallel workbench are gone.
    expect(screen.queryByRole('button', { name: 'Open workbench' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    const nav = screen.getByRole('navigation', { name: 'Lab modes' })
    expect(within(nav).getAllByRole('button')).toHaveLength(2)

    await openMaterial()

    expect(screen.getByRole('heading', { name: 'Westworld S01E01' })).toBeInTheDocument()
    expect(screen.queryByText(/Voice workbench/)).toBeNull()
    // No editor overlay to close, no permanent inspector or compare column.
    expect(screen.queryByRole('button', { name: 'Done' })).toBeNull()
    expect(screen.queryByRole('complementary', { name: 'inspector' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'compare' })).toBeNull()
  })

  it('keeps the waveform, the crop action and the clean transcript on the source bench', async () => {
    mockLab()
    renderShell()
    await openMaterial()

    expect(screen.getByLabelText('waveform')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'ANALYZE whole' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Create crop' })).toBeInTheDocument()
    expect(screen.getByLabelText('source transcript')).toHaveValue(
      'these violent delights have violent ends',
    )
    // The source-local speaker binding the deleted surface kept in its assets column.
    expect(screen.getByLabelText('Speaker 1 voice')).toBeInTheDocument()
  })

  it('keeps the processor home on the enrolled source and parks AuK', async () => {
    const { fetchMock } = mockLab()
    renderShell()
    await openMaterial()

    expect(screen.getByRole('button', { name: 'Resemble source' })).toBeEnabled()
    expect(screen.queryByText(/AuK/)).toBeNull()
    for (const name of ['Load AuK', 'Unload AuK', 'Generate candidate', 'diagnostics']) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    for (const intent of ['CLEAN', 'ISOLATE', 'PERFORM', 'EDIT', 'SYNTHESIZE']) {
      expect(screen.queryByRole('button', { name: intent })).toBeNull()
    }
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/auk'))).toEqual([])
  })

  it('cuts a crop child and keeps it beside its parent', async () => {
    const { calls } = mockLab()
    renderShell()
    await openMaterial()

    fireEvent.click(screen.getByRole('button', { name: /S1 0\.00s/ }))
    fireEvent.change(screen.getByLabelText('Crop name'), { target: { value: 'tight crop' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create crop' }))

    const lineage = await screen.findByRole('list', { name: 'source lineage' })
    expect(lineage).toHaveTextContent('original → tight crop')
    // The parent source is still listed: nothing was cut in place.
    expect(screen.getByRole('button', { name: /Westworld S01E01/ })).toBeInTheDocument()
    expect(
      calls.some((call) => call.method === 'POST' && call.url.includes('/sources/vs_primary/crop')),
    ).toBe(true)
  })

  it('keeps the selected voice as the store tab, not a second workbench', async () => {
    mockLab()
    renderShell()
    fireEvent.click(screen.getByRole('button', { name: 'VOICE LAB' }))
    fireEvent.click(await screen.findByRole('button', { name: /Ford/ }))
    await screen.findByRole('region', { name: 'ORIGINALS' })

    const originals = screen.getByRole('region', { name: 'ORIGINALS' })
    expect(originals).toHaveTextContent('Westworld S01E01')
    expect(originals).toHaveTextContent('derived from')
    expect(originals).toHaveTextContent('★')
    expect(screen.getByRole('region', { name: 'GENERATIONS' })).toHaveTextContent('No takes yet')
    expect(screen.queryByText(/Voice workbench/)).toBeNull()
  })

  it('leaves the Breeze loader to the global runtime chrome', async () => {
    mockLab()
    renderShell()
    await openMaterial()

    expect(screen.queryByRole('button', { name: /Load Breeze/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Unload Breeze/ })).toBeNull()
    expect(await screen.findByTestId('runtime-status')).toBeInTheDocument()
  })
})
