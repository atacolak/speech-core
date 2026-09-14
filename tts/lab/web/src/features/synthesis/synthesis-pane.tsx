import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { AudioBar } from '@/components/audio-bar'
import {
  ApiError,
  artifactAudioUrl,
  fetchRuntime,
  fetchRuns,
  fetchSteerFixtures,
  fetchVoices,
  formatApiError,
  loadE2,
  planSteer,
  synthesize,
} from '@/lib/api'
import { toGenerationBody } from '@/lib/generation'
import { formatMs, formatSeconds } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/state/workspace'

export function SynthesisPane() {
  const client = useQueryClient()
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const selectedRunId = useWorkspace((state) => state.selectedRunId)
  const selectRun = useWorkspace((state) => state.selectRun)
  const text = useWorkspace((state) => state.text)
  const setText = useWorkspace((state) => state.setText)
  const steer = useWorkspace((state) => state.steer)
  const setSteer = useWorkspace((state) => state.setSteer)
  const generation = useWorkspace((state) => state.generation)
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: fetchRuntime, refetchInterval: 2000 })
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const runs = useQuery({
    queryKey: ['runs', selectedVoiceId],
    queryFn: () => fetchRuns(selectedVoiceId),
    enabled: Boolean(selectedVoiceId),
  })
  const fixtures = useQuery({ queryKey: ['steer-fixtures'], queryFn: fetchSteerFixtures })
  const selected = voices.data?.find((voice) => voice.id === selectedVoiceId)
  const load = useMutation({
    mutationFn: loadE2,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtime'] }),
  })
  const take = useMutation({
    mutationFn: synthesize,
    onSuccess: (result) => {
      selectRun(result.id)
      void client.invalidateQueries({ queryKey: ['runs'] })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'runtime_unloaded') {
        toast.error('Breeze TTS2 is unloaded.', {
          action: {
            label: 'Load Breeze TTS2',
            onClick: () => load.mutate(),
          },
        })
        return
      }
      if (error instanceof ApiError && error.code === 'live_call_active') {
        toast.error('Live call owns the engine.')
        return
      }
      toast.error(formatApiError(error))
    },
  })
  const plan = useMutation({
    mutationFn: planSteer,
    onSuccess: (body) => {
      setSteer(body.steer)
      toast.success('Delivery filled')
    },
    onError: (error) => toast.error(formatApiError(error)),
  })

  const liveCall = Boolean(runtime.data?.live_call_active)
  const voiceTakes = (runs.data ?? []).filter((run) => run.voice_id === selectedVoiceId)
  const selectedTake =
    take.data && take.data.id === selectedRunId
      ? take.data
      : voiceTakes.find((run) => run.id === selectedRunId)

  return (
    <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto bg-zinc-800 p-4">
      <label className="text-xs font-medium uppercase tracking-wide text-zinc-400">
        Say
        <textarea
          className="mt-1 min-h-28 w-full rounded-md border border-zinc-600 bg-zinc-950 px-3 py-2 text-sm leading-6 text-zinc-100"
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      </label>
      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-400">Delivery</span>
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs text-zinc-200"
            disabled={plan.isPending || !text.trim()}
            onClick={() => plan.mutate(text)}
            title="Generate steer"
          >
            <Sparkles className="h-3.5 w-3.5" />
            {plan.isPending ? 'Planning…' : ''}
          </button>
        </div>
        <textarea
          className="min-h-20 w-full rounded-md border border-zinc-600 bg-zinc-950 px-3 py-2 text-sm text-zinc-100"
          value={steer}
          onChange={(event) => setSteer(event.target.value)}
        />
        {fixtures.data && fixtures.data.length > 0 ? (
          <details className="mt-2 text-xs text-zinc-400">
            <summary className="cursor-pointer">Fixtures</summary>
            <select
              className="mt-1 w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm text-zinc-100"
              defaultValue=""
              onChange={(event) => {
                const fixture = fixtures.data?.find((item) => item.id === event.target.value)
                if (!fixture) return
                setText(fixture.text)
                setSteer(fixture.steer)
              }}
            >
              <option value="">pick a fixture…</option>
              {fixtures.data.map((fixture) => (
                <option key={fixture.id} value={fixture.id}>
                  {fixture.id}
                </option>
              ))}
            </select>
          </details>
        ) : null}
      </div>
      <div className="flex justify-center">
        <button
          type="button"
          className={cn(
            'rounded-md bg-zinc-100 px-6 py-2 text-sm font-semibold tracking-wide text-zinc-950 disabled:opacity-40',
          )}
          disabled={take.isPending || liveCall || !text.trim()}
          onClick={() => {
            if (!selectedVoiceId || liveCall) return
            take.mutate({
              text,
              steer,
              voice_profile_id: selectedVoiceId,
              generation: toGenerationBody(generation),
            })
          }}
        >
          {liveCall ? 'Live call owns the engine' : take.isPending ? 'Generating…' : 'Generate'}
        </button>
      </div>
      <div className="mt-auto rounded-md border border-zinc-600 bg-zinc-900 p-3">
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">Latest take</p>
        {selectedTake ? (
          <>
            <AudioBar label="" src={artifactAudioUrl(selectedTake.output_artifact_id)} />
            <p className="mt-2 text-xs text-zinc-400">
              {selectedTake.first_audio_ms != null
                ? `first audio ${formatMs(selectedTake.first_audio_ms)} · `
                : ''}
              cfg {generation.dual ? `dual ${generation.cfgRef}/${generation.cfgIns}` : generation.cfg} ·
              seed {generation.seed} · {formatSeconds(selectedTake.duration_s)}
            </p>
            <div className="mt-2 flex gap-3 text-xs">
              <a
                className="text-zinc-200 underline"
                href={artifactAudioUrl(selectedTake.output_artifact_id)}
                download={`${selectedTake.id}.wav`}
              >
                ↓ Download
              </a>
            </div>
          </>
        ) : (
          <p className="text-sm text-zinc-300">No takes yet.</p>
        )}
        <p className="mt-2 text-[11px] text-zinc-500">
          Voice: {selected ? selected.name : 'none selected'}
          {selected && selected.tags.includes('sample') ? ' · sample' : ''}
          {runtime.data?.state === 'unloaded' ? ' · Breeze TTS2 unloaded' : ''}
        </p>
      </div>
    </section>
  )
}
