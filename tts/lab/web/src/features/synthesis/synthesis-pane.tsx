import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { AudioBar } from '@/components/audio-bar'
import { nextPlayable, ProgressivePlayer } from '@/features/synthesis/progressive'
import {
  ApiError,
  artifactAudioUrl,
  cancelGenerate,
  fetchGenerateJob,
  fetchRuntime,
  fetchRuns,
  fetchSteerFixtures,
  fetchVoices,
  formatApiError,
  generateSegmentAudioUrl,
  loadE2,
  planSteer,
  reportGenerateCursor,
  startGenerate,
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
  const [jobId, setJobId] = useState<string | null>(null)
  const jobIdRef = useRef<string | null>(null)
  const playerRef = useRef<ProgressivePlayer | null>(null)
  const enqueuedRef = useRef<number[]>([])
  const playedRef = useRef<number[]>([])
  const cursorRef = useRef(-1)
  const snapshotRef = useRef<string | null>(null)
  const invalidatedRef = useRef<string | null>(null)
  const completedRef = useRef<string | null>(null)
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: fetchRuntime, refetchInterval: 2000 })
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const runs = useQuery({
    queryKey: ['runs', selectedVoiceId],
    queryFn: () => fetchRuns(selectedVoiceId),
    enabled: Boolean(selectedVoiceId),
  })
  const fixtures = useQuery({ queryKey: ['steer-fixtures'], queryFn: fetchSteerFixtures })
  const job = useQuery({
    queryKey: ['generate', jobId],
    queryFn: () => fetchGenerateJob(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => (query.state.data?.state === 'running' ? 500 : false),
  })
  const selected = voices.data?.find((voice) => voice.id === selectedVoiceId)
  const load = useMutation({
    mutationFn: loadE2,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtime'] }),
  })
  const start = useMutation({
    mutationFn: startGenerate,
    onSuccess: (result) => {
      jobIdRef.current = result.id
      setJobId(result.id)
      client.setQueryData(['generate', result.id], result)
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
  const jobBody = job.data ?? null
  const voiceTakes = (runs.data ?? []).filter((run) => run.voice_id === selectedVoiceId)
  const selectedTake = voiceTakes.find((run) => run.id === selectedRunId)

  /** The job is an immutable snapshot: every field that would shift a pending segment. */
  const snapshotValue = JSON.stringify({
    text,
    steer,
    voice_profile_id: selectedVoiceId,
    generation: toGenerationBody(generation),
  })

  // Drain each contiguously finished segment into the player, in index order.
  useEffect(() => {
    const player = playerRef.current
    if (!player || !jobBody) return
    let index = nextPlayable(jobBody.segments, enqueuedRef.current)
    while (index != null) {
      enqueuedRef.current = [...enqueuedRef.current, index]
      player.enqueue(index, generateSegmentAudioUrl(jobBody.id, index)).catch(() => {
        // Drop the claim so the next status poll retries this one segment.
        enqueuedRef.current = enqueuedRef.current.filter((item) => item !== index)
      })
      index = nextPlayable(jobBody.segments, enqueuedRef.current)
    }
  }, [jobBody])

  // An edit that changes the snapshot invalidates the whole job once. The cancel
  // never touches playback, so every produced segment stays audible.
  useEffect(() => {
    if (!jobId || jobBody?.state !== 'running' || snapshotRef.current === snapshotValue) {
      return
    }
    if (invalidatedRef.current === jobId) {
      return
    }
    invalidatedRef.current = jobId
    cancelGenerate(jobId)
      .then((result) => client.setQueryData(['generate', jobId], result))
      .catch((error) => toast.error(formatApiError(error)))
  }, [client, jobBody, jobId, snapshotValue])

  // Only a complete job becomes one ordinary take.
  useEffect(() => {
    if (jobBody?.state !== 'complete' || completedRef.current === jobBody.id) {
      return
    }
    completedRef.current = jobBody.id
    if (jobBody.run_id) {
      selectRun(jobBody.run_id)
    }
    void client.invalidateQueries({ queryKey: ['runs'] })
  }, [client, jobBody, selectRun])

  // Playback resources are client-owned; abandoning the job server-side is not.
  useEffect(() => () => playerRef.current?.stop(), [])

  const handleEnded = (index: number) => {
    if (!playedRef.current.includes(index)) {
      playedRef.current.push(index)
    }
    let reached = -1
    while (playedRef.current.includes(reached + 1)) {
      reached += 1
    }
    const activeJobId = jobIdRef.current
    if (!activeJobId || reached === cursorRef.current) {
      return
    }
    cursorRef.current = reached
    reportGenerateCursor(activeJobId, reached).catch(() => {
      // A lost cursor report only throttles generation; playback is unaffected.
    })
  }

  const begin = () => {
    if (!selectedVoiceId || liveCall) return
    playerRef.current?.stop()
    playerRef.current = new ProgressivePlayer(undefined, handleEnded)
    enqueuedRef.current = []
    playedRef.current = []
    cursorRef.current = -1
    invalidatedRef.current = null
    completedRef.current = null
    snapshotRef.current = snapshotValue
    start.mutate({
      text,
      steer,
      voice_profile_id: selectedVoiceId,
      generation: toGenerationBody(generation),
    })
  }

  const cancelJob = () => {
    if (!jobId) return
    cancelGenerate(jobId)
      .then((result) => client.setQueryData(['generate', jobId], result))
      .catch((error) => toast.error(formatApiError(error)))
  }

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
          aria-label="Delivery"
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
      <div className="flex justify-center gap-2">
        <button
          type="button"
          className={cn(
            'rounded-md bg-zinc-100 px-6 py-2 text-sm font-semibold tracking-wide text-zinc-950 disabled:opacity-40',
          )}
          disabled={start.isPending || liveCall || !text.trim()}
          onClick={begin}
        >
          {liveCall ? 'Live call owns the engine' : start.isPending ? 'Generating…' : 'Generate'}
        </button>
        {jobBody?.state === 'running' ? (
          <button
            type="button"
            className="rounded-md border border-zinc-500 px-4 py-2 text-sm text-zinc-200"
            onClick={cancelJob}
          >
            Cancel
          </button>
        ) : null}
      </div>
      {jobBody && jobBody.segments.length > 0 ? (
        <ol className="flex flex-wrap gap-2 text-xs">
          {[...jobBody.segments]
            .sort((a, b) => a.index - b.index)
            .map((segment) => (
              <li key={segment.index} className="rounded border border-zinc-600 px-2 py-1">
                <span className="text-zinc-500">{segment.index + 1}</span>
                <span className="ml-1 text-zinc-300">{segment.state}</span>
              </li>
            ))}
        </ol>
      ) : null}
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
