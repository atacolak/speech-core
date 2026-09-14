import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { InspectorPane } from '@/features/inspector/inspector-pane'
import { VoicesPane } from '@/features/voices/voices-pane'
import { useWorkspace } from '@/state/workspace'

function variantVoice(overrides: Record<string, unknown> = {}) {
  return {
    id: 'vp1',
    name: 'ata',
    tags: [],
    source_audio_artifact_id: 'art_src',
    original_artifact_id: 'art_src',
    original_format: 'wav',
    source_transcript: 'hello',
    keep_intervals: [{ start_s: 0, end_s: 11.5 }],
    effective_transcript: 'hello',
    active_reference_variant_id: 'rv_den',
    active_variant: {
      id: 'rv_den',
      voice_profile_id: 'vp1',
      kind: 'resemble',
      audio_artifact_id: 'art_den',
      duration_s: 11.5,
      stale: false,
    },
    variants: [],
    speaker_analysis: null,
    duration_s: 11.5,
    source_duration_s: 12,
    effective_duration_s: 11.5,
    created_at: '2026-09-12T00:00:00Z',
    updated_at: '2026-09-12T00:00:00Z',
    ...overrides,
  }
}

function stubLab(current: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo) => {
      const url = String(input)
      const body = url.includes('/api/runtime')
        ? { active_voice_id: null, lease_owner: null, phase: 'idle', leases: [] }
        : url.includes('/api/runs')
          ? { items: [] }
          : { items: [current] }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
}

function renderPane(node: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useWorkspace.setState({ selectedVoiceId: 'vp1' })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

function audioSources(container: HTMLElement) {
  return Array.from(container.querySelectorAll('audio')).map((node) =>
    node.getAttribute('src') ?? '',
  )
}

describe('reference variant chrome', () => {
  afterEach(() => {
    // The stub stays installed: panes refetch during teardown, and a relative URL
    // reaching the real fetch would throw after the run.
    useWorkspace.setState({ selectedVoiceId: null })
  })

  it('names a parked denoise variant without the resemble product name and plays its artifact', async () => {
    stubLab(variantVoice())
    renderPane(<VoicesPane />)
    const label = await screen.findByText(/reference: denoised/)
    const card = label.closest('div') as HTMLElement
    expect(screen.getByText(/▶ denoised/)).toBeInTheDocument()
    expect(screen.queryByText(/resemble/i)).toBeNull()
    expect(audioSources(card).some((src) => src.includes('art_den'))).toBe(true)
  })

  it('falls back to the keep crop and flags a stale denoise variant', async () => {
    stubLab(
      variantVoice({
        active_variant: {
          id: 'rv_den',
          voice_profile_id: 'vp1',
          kind: 'resemble',
          audio_artifact_id: 'art_den',
          duration_s: 11.5,
          stale: true,
        },
      }),
    )
    renderPane(<VoicesPane />)
    const label = await screen.findByText(/reference: denoised \(stale\)/)
    // The reference chrome is the card; the bench may list the same artifact on its own.
    const sources = audioSources(label.closest('div') as HTMLElement)
    expect(sources.some((src) => src.includes('art_den'))).toBe(false)
    expect(sources.some((src) => src.includes('/reference/audio'))).toBe(true)
  })

  it('reports the active reference kind in the settings pane', async () => {
    stubLab(variantVoice())
    renderPane(<InspectorPane />)
    expect(await screen.findByText(/^denoised · /)).toBeInTheDocument()
    expect(screen.queryByText(/resemble/i)).toBeNull()
  })

  it('names a parked auk variant by a neutral word, not its task or the product', async () => {
    stubLab(
      variantVoice({
        active_reference_variant_id: 'rv_auk',
        active_variant: {
          id: 'rv_auk',
          voice_profile_id: 'vp1',
          kind: 'auk',
          audio_artifact_id: 'art_auk',
          duration_s: 11.5,
          auk_task: 'enhance',
          auk_precision: 'bf16',
          stale: false,
        },
      }),
    )
    renderPane(<VoicesPane />)
    const label = await screen.findByText(/reference: candidate/)
    expect(screen.getByText(/▶ candidate/)).toBeInTheDocument()
    // Exact text only: prose may describe processing, but no asset is *named* by a task word.
    expect(screen.queryByText('enhance')).toBeNull()
    expect(screen.queryByText(/auk/i)).toBeNull()
    const card = label.closest('div') as HTMLElement
    expect(audioSources(card).some((src) => src.includes('art_auk'))).toBe(true)
  })

  it('reports the parked auk variant as a candidate in the settings pane', async () => {
    stubLab(
      variantVoice({
        active_reference_variant_id: 'rv_auk',
        active_variant: {
          id: 'rv_auk',
          voice_profile_id: 'vp1',
          kind: 'auk',
          audio_artifact_id: 'art_auk',
          duration_s: 11.5,
          stale: false,
        },
      }),
    )
    renderPane(<InspectorPane />)
    expect(await screen.findByText(/^candidate · /)).toBeInTheDocument()
    expect(screen.queryByText(/auk/i)).toBeNull()
  })
})
