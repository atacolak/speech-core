import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TakeCard, TakesSection } from '@/features/synthesis/takes-section'
import type { RunItem, Voice } from '@/lib/api'
import { renameRun } from '@/lib/api'
import { useWorkspace } from '@/state/workspace'

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    renameRun: vi.fn(),
  }
})

const voice = {
  id: 'voice-1',
  name: 'ata',
  take_limit: 4,
} as Voice

const newest = {
  id: 'run-newest',
  voice_id: voice.id,
  output_artifact_id: 'artifact-newest',
  duration_s: 3,
  latency_ms: 100,
  first_audio_ms: 20,
  rating: null,
  tags: [],
  request_snapshot: { text: 'newest words' },
} as RunItem

const older = {
  id: 'run-older',
  voice_id: voice.id,
  output_artifact_id: 'artifact-older',
  duration_s: 2,
  latency_ms: 90,
  first_audio_ms: 15,
  rating: null,
  tags: [],
  request_snapshot: { text: 'older words' },
} as RunItem

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  useWorkspace.setState({ selectedVoiceId: voice.id, selectedRunId: null })
  return render(
    <QueryClientProvider client={client}>
      <TakesSection voice={voice} takes={[newest, older]} pending={false} />
    </QueryClientProvider>,
  )
}

describe('takes section', () => {
  it('shows the newest take once under Latest take and the rest under Takes', () => {
    renderSection()

    const latest = screen.getByRole('group', { name: 'Latest take' })
    const takes = screen.getByRole('group', { name: 'Takes' })
    expect(within(latest).getByText('newest words')).toBeInTheDocument()
    expect(within(takes).queryByText('newest words')).toBeNull()
    expect(within(takes).getByText('older words')).toBeInTheDocument()
    expect(within(takes).getByText(/cap/i)).toBeInTheDocument()
    expect(screen.getAllByText('newest words')).toHaveLength(1)
  })

  it('does not select a take card on click', () => {
    renderSection()
    fireEvent.click(screen.getByText('older words'))
    expect(document.querySelector('.border-emerald-400')).toBeNull()
    expect(useWorkspace.getState().selectedRunId).toBeNull()
  })


  it('titles a take with an untitled field, never the run hash', () => {
    const hashed = { ...older, id: 'take_run_deadbeef', name: null }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <TakeCard
          inspected={false}
          onDelete={() => undefined}
          onInspect={() => undefined}
          onSave={() => undefined}
          run={hashed}
          voice={voice}
        />
      </QueryClientProvider>,
    )

    const title = screen.getByPlaceholderText('Untitled')
    expect(title).toHaveValue('')
    expect(screen.queryByDisplayValue(/take_run_/)).toBeNull()
    expect(screen.queryByRole('heading', { name: /take_run_/ })).toBeNull()
  })

  it('commits a take title on blur and Enter', async () => {
    vi.mocked(renameRun).mockResolvedValue({ ...older, name: 'morning take' })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <TakeCard
          inspected={false}
          onDelete={() => undefined}
          onInspect={() => undefined}
          onSave={() => undefined}
          run={older}
          voice={voice}
        />
      </QueryClientProvider>,
    )

    const title = screen.getByPlaceholderText('Untitled')
    fireEvent.change(title, { target: { value: 'morning take' } })
    fireEvent.blur(title)
    await waitFor(() => expect(renameRun).toHaveBeenCalledWith(older.id, 'morning take'))

    vi.mocked(renameRun).mockClear()
    fireEvent.change(title, { target: { value: 'evening take' } })
    fireEvent.keyDown(title, { key: 'Enter' })
    await waitFor(() => expect(renameRun).toHaveBeenCalledWith(older.id, 'evening take'))
  })
})

afterEach(() => {
  vi.mocked(renameRun).mockReset()
})
