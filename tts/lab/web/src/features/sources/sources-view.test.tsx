import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Toaster } from 'sonner'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MediaSource } from '@/features/sources/sources-api'
import { SourcesView } from '@/features/sources/sources-view'

type Call = { method: string; url: string; body: unknown }

function source(overrides: Partial<MediaSource> = {}): MediaSource {
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
    coverage: [],
    analyses: [],
    speakers: [],
    clips: [],
    ...overrides,
  }
}

const WESTWORLD = source()
const INTERVIEW = source({
  id: 'src_interview',
  origin: 'interview.wav',
  title: 'interview.wav',
  duration_s: 90,
  audio_artifact_id: 'art_src2',
})

function stubLab(items: MediaSource[], status = 201) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({
        method,
        url,
        body:
          init?.body instanceof FormData
            ? Object.fromEntries(init.body.entries())
            : init?.body
              ? String(init.body)
              : null,
      })
      if (method === 'POST') {
        if (status >= 400) {
          return new Response(JSON.stringify({ detail: 'not_implemented' }), {
            status,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return new Response(JSON.stringify(WESTWORLD), {
          status,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ items }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  return calls
}

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SourcesView />
      <Toaster />
    </QueryClientProvider>,
  )
}

describe('sources view', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('says so when there are no sources yet', async () => {
    stubLab([])
    renderView()
    expect(await screen.findByText(/No sources yet/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'ANALYZE whole' })).toBeNull()
  })

  it('lists sources by title and picks one for the bench', async () => {
    stubLab([WESTWORLD, INTERVIEW])
    renderView()
    const westworld = await screen.findByRole('button', { name: /Westworld S01E01/ })
    expect(screen.getByRole('button', { name: /interview\.wav/ })).toBeInTheDocument()
    expect(screen.getAllByText(/not analyzed/).length).toBe(2)
    expect(screen.queryByLabelText('waveform')).toBeNull()

    fireEvent.click(westworld)

    expect(await screen.findByLabelText('waveform')).toBeInTheDocument()
    expect(screen.getByLabelText('analyzed coverage')).toHaveAttribute('data-coverage', '0')
    expect(screen.getByRole('button', { name: 'ANALYZE whole' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'ANALYZE selection' })).toBeDisabled()
  })

  it('counts the analyzed speakers and clips of a source in the list', async () => {
    stubLab([
      source({
        coverage: [{ start_s: 0, end_s: 5 }],
        analyses: [
          {
            id: 'sa_1',
            start_s: 0,
            end_s: 5,
            processor: 'vibevoice',
            model_id: null,
            config: {},
            result: { segments: [] },
            created_at: '2026-09-14T00:00:00Z',
          },
        ],
        speakers: [
          { local_id: 'S1', label: 'Speaker 1', duration_s: 5, mapped_voice_id: null },
        ],
        clips: [
          {
            id: 'clip_1',
            source_id: 'src_ww',
            speaker_local_id: 'S1',
            voice_id: null,
            ranges: [{ start_s: 0, end_s: 2 }],
            segments: [],
            audio_artifact_id: 'art_clip',
            clean_transcript: 'one',
            created_at: '2026-09-14T00:00:00Z',
          },
        ],
      }),
    ])
    renderView()
    expect(await screen.findByText(/1 speaker\(s\) · 1 clip\(s\)/)).toBeInTheDocument()
  })

  it('submits a URL source without transcribing anything', async () => {
    const calls = stubLab([], 501)
    renderView()
    const url = await screen.findByLabelText('Source URL')
    fireEvent.change(url, { target: { value: 'https://www.youtube.com/watch?v=fixture' } })
    fireEvent.click(screen.getByRole('button', { name: '+ Add source' }))

    await waitFor(() => {
      expect(calls.some((call) => call.method === 'POST' && call.url.includes('/api/sources'))).toBe(
        true,
      )
    })
    const posted = calls.find((call) => call.method === 'POST')
    expect(posted?.body).toEqual({ url: 'https://www.youtube.com/watch?v=fixture' })
    expect((await screen.findAllByText(/source ingest is not available/i)).length).toBeGreaterThan(0)
    expect(calls.filter((call) => call.url.includes('analyze'))).toEqual([])
  })

  it('offers a local file without a URL', async () => {
    const calls = stubLab([])
    renderView()
    const file = await screen.findByLabelText('Source file')
    const take = new File(['riff'], 'take.wav', { type: 'audio/wav' })
    fireEvent.change(file, { target: { files: [take] } })
    await waitFor(() => {
      expect(calls.some((call) => call.method === 'POST' && call.url.includes('/api/sources'))).toBe(
        true,
      )
    })
    expect(calls.find((call) => call.method === 'POST')?.body).toEqual({ file: take })
  })
})
