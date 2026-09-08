import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '@/app'

const RUNTIME = {
  selected: 'E2',
  status: 'ready',
  leftover_parked: true,
  not_a_pin_swap: true,
  voicecat_path: false,
}

function renderApp(ui: ReactElement = <App />) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

describe('TTS lab shell', () => {
  it('renders the top bar and three instrument panes', () => {
    renderApp()
    expect(screen.getByRole('banner')).toHaveTextContent('TTS lab')
    expect(screen.getByRole('heading', { name: 'Voices' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Synthesis' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Inspector' })).toBeInTheDocument()
  })
})

describe('runtime indicator', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(RUNTIME), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows the selected E2 runtime and leftover parked', async () => {
    renderApp()
    expect(await screen.findByText('E2')).toBeInTheDocument()
    expect(screen.getByText(/leftover parked/i)).toBeInTheDocument()
    expect(screen.getByText(/not a pin swap/i)).toBeInTheDocument()
    expect(screen.getByTestId('runtime-status')).toHaveAttribute('data-status', 'ready')
  })

  it('shows an error state when runtime fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network')))
    renderApp()
    await waitFor(() => {
      expect(screen.getByTestId('runtime-status')).toHaveAttribute(
        'data-status',
        'error',
      )
    })
    expect(screen.getByRole('banner')).toHaveTextContent('TTS lab')
  })
})
