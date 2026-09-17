import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { InspectorPane } from '@/features/inspector/inspector-pane'
import { DEFAULT_GENERATION } from '@/lib/generation'
import { useWorkspace } from '@/state/workspace'

function renderPane() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <InspectorPane />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  useWorkspace.setState({
    selectedVoiceId: null,
    hydratedVoiceId: null,
    generation: { ...DEFAULT_GENERATION },
  })
})

describe('inspector generation controls', () => {
  it('starts with Settings collapsed in workspace', () => {
    expect(useWorkspace.getState().settingsOpen).toBe(false)
  })

  it('renders Advanced generation as a title, not a disclosure', () => {
    renderPane()
    expect(screen.getByText('Advanced generation')).toBeInTheDocument()
    expect(screen.queryByText('Guidance')).toBeNull()
    expect(screen.queryByRole('group', { name: 'Advanced generation' })).toBeNull()
    expect(document.querySelector('summary')).toBeTruthy()
    expect(screen.queryByText('Advanced generation')?.closest('summary')).toBeNull()
  })

  it('puts experimental dual-cfg at the end of Advanced and swaps cfg for two sliders', () => {
    renderPane()
    const cfg = screen.getByLabelText(/^cfg$/i)
    expect(cfg).toBeInTheDocument()
    expect(screen.queryByLabelText(/^reference$/i)).toBeNull()
    const dual = screen.getByLabelText(/experimental dual-cfg/i)
    fireEvent.click(dual)
    expect(screen.queryByLabelText(/^cfg$/i)).toBeNull()
    expect(screen.getByLabelText(/^reference$/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/^instruction$/i)).toBeInTheDocument()
    const advanced = screen.getByText('Advanced generation')
    expect(advanced.compareDocumentPosition(dual) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
