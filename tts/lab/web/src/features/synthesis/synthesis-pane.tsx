import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { toast } from 'sonner'
import { TakePlayer } from '@/components/take-player'
import {
  ApiError,
  artifactAudioUrl,
  fetchRuntime,
  fetchRuns,
  fetchSteerFixtures,
  fetchVoices,
  formatApiError,
  loadE2,
  openGenerateStream,
  planSteer,
  stopGenerate,
} from '@/lib/api'
import type { RunItem } from '@/lib/api'
import { formatMs, formatSeconds } from '@/lib/format'
import { toGenerationBody } from '@/lib/generation'
import { decodePcmWav, PcmTimeline } from '@/lib/pcm-timeline'
import { highlightAt } from '@/lib/spoken-alignment'
import type { Highlight } from '@/lib/spoken-alignment'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/state/workspace'

/** Post-take metadata refresh only: alignment settles long before the next take. */
const ALIGNMENT_POLL_MS = 1000

/**
 * Only a take Parakeet is still hearing needs the run query kept warm. Runs
 * arrive newest first, so without a selected pointer the newest row is the take
 * the GENERATE column is showing.
 */
function alignmentPending(
  runs: RunItem[] | undefined,
  latestTakeId: string | null | undefined,
): boolean {
  const rows = runs ?? []
  const observed = latestTakeId == null ? rows[0] : rows.find((run) => run.id === latestTakeId)
  return observed?.alignment?.status === 'pending'
}

/**
 * The newest ready-aligned take's measured character rate, excluding the take
 * being played. It is the only evidence a not-yet-aligned take may borrow.
 */
function priorAlignedCharsPerSecond(
  runs: RunItem[],
  playedRunId: string | undefined,
): number | null {
  for (const run of runs) {
    if (run.id === playedRunId) {
      continue
    }
    const words = run.alignment?.status === 'ready' ? (run.alignment.words ?? []) : []
    if (words.length === 0) {
      continue
    }
    const characters = words.reduce((total, word) => total + word.text.length, 0)
    const span = words[words.length - 1].end_s - words[0].start_s
    if (characters > 0 && span > 0) {
      return characters / span
    }
  }
  return null
}

/**
 * Say with one word washed and titled, mirrored over the textarea so the words
 * on screen are the words the audio is at. Unmatched Say words are untouched.
 */
function mirroredSay(say: string, highlight: Highlight | null): ReactNode[] {
  const nodes: ReactNode[] = []
  let wordIndex = 0
  for (const part of say.split(/(\s+)/)) {
    if (part.length === 0) {
      continue
    }
    if (/^\s+$/u.test(part)) {
      nodes.push(part)
      continue
    }
    nodes.push(
      wordIndex === highlight?.wordIndex ? (
        <mark
          className="rounded-sm bg-[rgba(56,189,248,0.18)] text-transparent shadow-[0_0_0_1px_rgba(56,189,248,0.85)]"
          key={wordIndex}
          title={highlight.title}
        >
          {part}
        </mark>
      ) : (
        part
      ),
    )
    wordIndex += 1
  }
  return nodes
}

export function SynthesisPane() {
  const client = useQueryClient()
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const text = useWorkspace((state) => state.text)
  const setText = useWorkspace((state) => state.setText)
  const steer = useWorkspace((state) => state.steer)
  const setSteer = useWorkspace((state) => state.setSteer)
  const generation = useWorkspace((state) => state.generation)
  const hydrateGeneration = useWorkspace((state) => state.hydrateGeneration)
  const [timeline, setTimeline] = useState<PcmTimeline | null>(null)
  const [autoplay, setAutoplay] = useState(false)
  const [takeSeq, setTakeSeq] = useState(0)
  const [streamId, setStreamId] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  // Coarse parent state: the player reports only when the audible position moves.
  const [playheadS, setPlayheadS] = useState(0)
  // The timeline is appended to in place, so a duration read is what re-renders
  // the player after every chunk.
  const [, setDurationS] = useState(0)
  const generatingRef = useRef(false)
  const streamIdRef = useRef<string | null>(null)
  const restoredRef = useRef<string | null>(null)
  const streamedVoiceRef = useRef<string | null>(null)
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: fetchRuntime, refetchInterval: 2000 })
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const selected = voices.data?.find((voice) => voice.id === selectedVoiceId)
  const runs = useQuery({
    queryKey: ['runs', selectedVoiceId],
    queryFn: () => fetchRuns(selectedVoiceId),
    enabled: Boolean(selectedVoiceId),
    refetchInterval: (query) =>
      alignmentPending(query.state.data, selected?.latest_take_id) ? ALIGNMENT_POLL_MS : false,
  })
  const fixtures = useQuery({ queryKey: ['steer-fixtures'], queryFn: fetchSteerFixtures })
  const load = useMutation({
    mutationFn: loadE2,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtime'] }),
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
  const latestRun = (runs.data ?? []).find((run) => run.id === selected?.latest_take_id)
  const alignment = latestRun?.alignment ?? null
  const priorRate = useMemo(
    () => priorAlignedCharsPerSecond(runs.data ?? [], latestRun?.id),
    [runs.data, latestRun?.id],
  )
  const highlight = useMemo(
    () => highlightAt(playheadS, text, alignment, priorRate),
    [alignment, playheadS, priorRate, text],
  )

  // The knobs follow the selected profile, not whether any drawer is mounted.
  useEffect(() => {
    hydrateGeneration(selected ?? null)
  }, [hydrateGeneration, selected])

  // A new voice shows only its own take; neither server pointer moves.
  useEffect(() => {
    streamedVoiceRef.current = null
    setTimeline(null)
    setAutoplay(false)
    setPlayheadS(0)
  }, [selectedVoiceId])

  // Restore the voice's latest ordinary take from the server's pointer.
  useEffect(() => {
    if (generating || streamedVoiceRef.current === selectedVoiceId) {
      return
    }
    if (!latestRun) {
      return
    }
    const artifactId = latestRun.output_artifact_id
    const key = `${latestRun.id}:${artifactId}`
    if (restoredRef.current === key) {
      return
    }
    restoredRef.current = key
    let cancelled = false
    fetch(artifactAudioUrl(artifactId))
      .then((response) => (response.ok ? response.arrayBuffer() : null))
      .then((bytes) => {
        const decoded = bytes === null ? null : decodePcmWav(bytes)
        if (cancelled || decoded === null) {
          return
        }
        setAutoplay(false)
        setTimeline(decoded)
      })
      .catch(() => {
        // A take that will not decode simply leaves the player empty.
      })
    return () => {
      cancelled = true
    }
  }, [generating, latestRun, selectedVoiceId])

  const begin = async () => {
    if (!selectedVoiceId || liveCall || generatingRef.current) {
      return
    }
    generatingRef.current = true
    streamedVoiceRef.current = selectedVoiceId
    setGenerating(true)
    setAutoplay(true)
    setTakeSeq((seq) => seq + 1)
    setTimeline(null)
    setPlayheadS(0)
    try {
      const stream = await openGenerateStream({
        text,
        steer,
        voice_profile_id: selectedVoiceId,
        generation: toGenerationBody(generation),
      })
      streamIdRef.current = stream.id
      setStreamId(stream.id)
      const live = new PcmTimeline(stream.sampleRate)
      setTimeline(live)
      for await (const chunk of stream.chunks) {
        setDurationS(live.appendS16(chunk))
      }
    } catch (error) {
      if (error instanceof ApiError && error.code === 'runtime_unloaded') {
        toast.error('Breeze TTS2 is unloaded.', {
          action: {
            label: 'Load Breeze TTS2',
            onClick: () => load.mutate(),
          },
        })
      } else if (error instanceof ApiError && error.code === 'live_call_active') {
        toast.error('Live call owns the engine.')
      } else {
        toast.error(formatApiError(error))
      }
    } finally {
      generatingRef.current = false
      streamIdRef.current = null
      setStreamId(null)
      setGenerating(false)
      // The stream recorded an ordinary take; pick up its server-side pointer.
      void client.invalidateQueries({ queryKey: ['voices'] })
      void client.invalidateQueries({ queryKey: ['runs'] })
    }
  }

  const stopStream = () => {
    const id = streamIdRef.current
    if (!id) {
      return
    }
    // Stop ends unborn work; the reader is never aborted, because the server
    // ends the body only after it records the produced PCM as the take.
    stopGenerate(id).catch((error: unknown) => toast.error(formatApiError(error)))
  }

  return (
    <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto bg-zinc-900 p-4">
      <div>
        <label
          className="text-xs font-medium uppercase tracking-wide text-zinc-400"
          htmlFor="say-text"
        >
          Say
        </label>
        <div className="relative mt-1">
          <textarea
            className="min-h-70 w-full rounded-md border border-zinc-800 bg-zinc-950/40 px-3 py-2 text-[17px] leading-6 text-zinc-100"
            id="say-text"
            spellCheck={false}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          {/* The spoken word is drawn by the real text, so this layer stays silent. */}
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words rounded-md border border-transparent px-3 py-2 text-[17px] leading-6 text-transparent"
          >
            {mirroredSay(text, highlight)}
          </div>
        </div>
      </div>
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
          className="min-h-20 w-full rounded-md border border-zinc-800 bg-zinc-950/40 px-3 py-2 text-[17px] leading-6 text-zinc-100"
          spellCheck={false}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          value={steer}
          onChange={(event) => setSteer(event.target.value)}
        />
        {fixtures.data && fixtures.data.length > 0 ? (
          <details className="mt-2 text-xs text-zinc-400">
            <summary className="cursor-pointer">Fixtures</summary>
            <select
              className="mt-1 w-full rounded-md border border-zinc-800 bg-zinc-950/40 px-2 py-1 text-sm text-zinc-100"
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
          disabled={generating || liveCall || !text.trim()}
          onClick={() => void begin()}
        >
          {liveCall ? 'Live call owns the engine' : 'Generate'}
        </button>
        {streamId ? (
          <button
            type="button"
            className="rounded-md border border-zinc-500 px-4 py-2 text-sm text-zinc-200"
            onClick={stopStream}
          >
            Stop
          </button>
        ) : null}
      </div>
      {timeline ? (
        <TakePlayer
          key={takeSeq}
          timeline={timeline}
          autoplay={autoplay}
          live={generating}
          onPlayheadChange={setPlayheadS}
        />
      ) : null}
      <div className="mt-auto rounded-md border border-zinc-800 bg-zinc-950/40 p-3">
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">Latest take</p>
        {latestRun ? (
          <>
            <p className="mt-2 text-xs text-zinc-400">
              {latestRun.first_audio_ms != null
                ? `first audio ${formatMs(latestRun.first_audio_ms)} · `
                : ''}
              cfg {generation.dual ? `dual ${generation.cfgRef}/${generation.cfgIns}` : generation.cfg} ·
              seed {generation.seed} · {formatSeconds(latestRun.duration_s)}
            </p>
            <div className="mt-2 flex gap-3 text-xs">
              <a
                className="text-zinc-200 underline"
                href={artifactAudioUrl(latestRun.output_artifact_id)}
                download={`${latestRun.id}.wav`}
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
