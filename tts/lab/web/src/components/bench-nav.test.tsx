import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
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

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function labFetch(sources: unknown[] = []) {
  return vi.fn(async (input: RequestInfo) => {
    const url = String(input)
    if (url.includes('/api/runtime')) {
      return json(RUNTIME)
    }
    if (url.includes('/api/sources')) {
      return json({ items: sources })
    }
    if (url.includes('/api/voices')) {
      return json({ items: [] })
    }
    if (url.includes('/api/runs')) {
      return json({ items: [] })
    }
    if (url.includes('/api/fixtures/steer')) {
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

describe('lab bench nav', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', labFetch())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('offers Voices | Sources and swaps the surface', async () => {
    renderApp()
    const voices = screen.getByRole('button', { name: 'Voices' })
    const sources = screen.getByRole('button', { name: 'Sources' })
    expect(voices).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { name: 'Voices' })).toBeInTheDocument()

    fireEvent.click(sources)

    expect(sources).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { name: 'Sources' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Voices' })).not.toBeInTheDocument()
    expect(await screen.findByText(/No sources yet/i)).toBeInTheDocument()

    fireEvent.click(voices)

    expect(screen.getByRole('heading', { name: 'Voices' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Sources' })).not.toBeInTheDocument()
  })

  it('keeps AuK chrome off the lab surfaces', async () => {
    renderApp()
    expect(await screen.findByText(/No voices yet/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /auk/i })).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^(CLEAN|ISOLATE|PERFORM|EDIT)$/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Sources' }))

    expect(screen.queryByRole('button', { name: /auk/i })).toBeNull()
    expect(screen.queryByRole('radio', { name: /bf16|int8/i })).toBeNull()
  })
})
