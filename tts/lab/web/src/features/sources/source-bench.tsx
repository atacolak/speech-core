import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import {
  type MediaSource,
  type SourceRange,
  type SourceTurn,
  analyzeSource,
  extractClips,
  fetchPeaks,
  isUnavailable,
  mapSourceSpeaker,
} from '@/features/sources/sources-api'
import {
  ApiError,
  type Voice,
  type VoiceArtifact,
  type VoiceSource,
  cropVoiceSource,
  denoiseVoiceSource,
  fetchVoices,
  formatApiError,
  patchVoiceSource,
} from '@/lib/api'
import { useWorkspace } from '@/state/workspace'
import { cn } from '@/lib/utils'

const ANALYZE_UNAVAILABLE = 'Source analysis is not available in this lab build yet.'
const EXTRACT_UNAVAILABLE = 'Clip extraction is not available in this lab build yet.'
const MAP_UNAVAILABLE = 'Speaker mapping is not available in this lab build yet.'

/** One enrolled source the surgery can write to, with the voice that owns it. */
type EnrolledSource = { voice: Voice; source: VoiceSource }

/** Operator word for a lineage row: its name when it has one, else its kind. */
function artifactWord(artifact: VoiceArtifact): string {
  return artifact.name?.trim() || artifact.kind
}

/** The enrolled sources the material's mapped voices own, in payload order. */
function enrolledSources(mapped: Voice[]): EnrolledSource[] {
  return mapped.flatMap((voice) => (voice.sources ?? []).map((source) => ({ voice, source })))
}

function turnKey(turn: SourceTurn): string {
  return `${turn.speaker_id}:${turn.start_s}-${turn.end_s}`
}

/** Why the picked range cannot be analyzed, or null when it can. Blank fields are simply unpicked. */
function rangeIssue(start: number | null, end: number | null, duration_s: number): string | null {
  if (start === null || end === null) {
    return null
  }
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    return 'range must be a number'
  }
  if (end <= start) {
    return 'range ends before it starts'
  }
  if (start < 0 || end > duration_s) {
    return 'range runs past the source'
  }
  return null
}

function WaveformPeaks({
  peaks,
  coverage,
  duration,
  picked,
}: {
  peaks: number[]
  coverage: SourceRange[]
  duration: number
  picked: SourceRange | null
}) {
  const percent = (seconds: number) => (seconds / Math.max(duration, 0.001)) * 100
  const count = Math.max(peaks.length, 1)
  return (
    <div
      aria-label="waveform"
      className="relative h-28 w-full overflow-hidden rounded-md border border-zinc-700 bg-zinc-950"
    >
      <svg className="absolute inset-0 h-full w-full" preserveAspectRatio="none" viewBox={`0 0 ${count} 100`}>
        {peaks.length === 0 ? (
          <line x1="0" y1="50" x2={count} y2="50" stroke="#52525b" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        ) : (
          peaks.map((value, index) => {
            const height = Math.max(2, Math.min(100, value * 100))
            return (
              <rect
                fill="#a1a1aa"
                height={height}
                key={index}
                width={1}
                x={index}
                y={(100 - height) / 2}
              />
            )
          })
        )}
      </svg>
      {coverage.map((range) => (
        <div
          className="pointer-events-none absolute top-0 h-full bg-emerald-400/15"
          key={`${range.start_s}-${range.end_s}`}
          style={{ left: `${percent(range.start_s)}%`, width: `${percent(range.end_s - range.start_s)}%` }}
        />
      ))}
      {picked ? (
        <div
          data-testid="selection-marker"
          className="pointer-events-none absolute top-0 h-full border-x-2 border-sky-400/80 bg-sky-400/10"
          style={{ left: `${percent(picked.start_s)}%`, width: `${percent(picked.end_s - picked.start_s)}%` }}
        />
      ) : null}
    </div>
  )
}

export function SourceBench({ source }: { source: MediaSource }) {
  const client = useQueryClient()
  const selectVoice = useWorkspace((state) => state.selectVoice)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [enrolledKey, setEnrolledKey] = useState<string | null>(null)
  const [cropName, setCropName] = useState('')
  const [draft, setDraft] = useState('')
  const [capError, setCapError] = useState<string | null>(null)
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const peaks = useQuery({
    queryKey: ['source-peaks', source.waveform_artifact_id],
    queryFn: () => fetchPeaks(source.waveform_artifact_id as string),
    enabled: Boolean(source.waveform_artifact_id),
  })

  const turns = source.analyses.flatMap((analysis) => analysis.result.segments ?? [])
  const covered = source.coverage.reduce((total, range) => total + (range.end_s - range.start_s), 0)
  const fraction = source.duration_s > 0 ? Number((covered / source.duration_s).toFixed(4)) : 0

  const start = from.trim() === '' ? null : Number(from)
  const end = to.trim() === '' ? null : Number(to)
  const issue = rangeIssue(start, end, source.duration_s)
  const pickedRange =
    issue === null && start !== null && end !== null ? { start_s: start, end_s: end } : null

  const chosen = picked.flatMap((key) => {
    const turn = turns.find((item) => turnKey(item) === key)
    return turn ? [turn] : []
  })
  const speakers = new Set(chosen.map((turn) => turn.speaker_id))
  const mixedSpeakers = speakers.size > 1
  const ranges: SourceRange[] = [...chosen]
    .sort((left, right) => left.start_s - right.start_s)
    .map((turn) => ({ start_s: turn.start_s, end_s: turn.end_s }))

  // The voices this material's speakers are mapped onto: the only owners of its surgery.
  const mappedIds = new Set(
    source.speakers.flatMap((speaker) =>
      speaker.mapped_voice_id === null ? [] : [speaker.mapped_voice_id],
    ),
  )
  const enrolled = enrolledSources(
    (voices.data ?? []).filter((voice) => mappedIds.has(voice.id)),
  )
  const target =
    enrolled.find((item) => item.source.id === enrolledKey) ??
    enrolled.find((item) => item.source.artifact_id === item.voice.source_audio_artifact_id) ??
    enrolled[0]
  const lineage = target
    ? (target.voice.artifacts ?? []).filter((item) => item.source_id === target.source.id)
    : []
  const isPrimary = Boolean(
    target && target.source.artifact_id === target.voice.source_audio_artifact_id,
  )

  useEffect(() => {
    setDraft(target?.source.transcript ?? '')
  }, [target?.source.id, target?.source.transcript])

  const analyze = useMutation({
    mutationFn: (range: Parameters<typeof analyzeSource>[1]) => analyzeSource(source.id, range),
    onSuccess: (result) => {
      toast.success(
        result.analyses_added > 0
          ? `Stored ${result.analyses_added} analyzed range(s)`
          : 'That range is already analyzed',
      )
      void client.invalidateQueries({ queryKey: ['sources'] })
    },
    onError: (error) => toast.error(isUnavailable(error) ? ANALYZE_UNAVAILABLE : 'Analysis failed.'),
  })
  const extract = useMutation({
    mutationFn: () =>
      extractClips(source.id, {
        speaker_local_id: chosen[0].speaker_id,
        ranges,
      }),
    onSuccess: (clip) => {
      toast.success(`Clip ${clip.ranges.length} turn(s) extracted`)
      void client.invalidateQueries({ queryKey: ['sources'] })
      void client.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (error) => {
      // A full voice refuses before it materializes anything: say so where it happened.
      if (error instanceof ApiError && error.code === 'source_limit_reached') {
        setCapError(formatApiError(error))
        return
      }
      toast.error(isUnavailable(error) ? EXTRACT_UNAVAILABLE : 'Extract failed.')
    },
  })
  const map = useMutation({
    mutationFn: (input: { localId: string; voiceId: string | null }) =>
      mapSourceSpeaker(source.id, input.localId, input.voiceId),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['sources'] }),
    onError: (error) => toast.error(isUnavailable(error) ? MAP_UNAVAILABLE : 'Mapping failed.'),
  })
  const saveTranscript = useMutation({
    mutationFn: (input: { voiceId: string; sourceId: string; transcript: string }) =>
      patchVoiceSource(input.voiceId, input.sourceId, { transcript: input.transcript }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['voices'] }),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const crop = useMutation({
    mutationFn: (input: { voiceId: string; sourceId: string; intervals: SourceRange[]; name: string }) =>
      cropVoiceSource(input.voiceId, input.sourceId, {
        intervals: input.intervals,
        name: input.name,
      }),
    onSuccess: () => {
      toast.success('Crop created as a child of this source')
      void client.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })
  const resemble = useMutation({
    mutationFn: (input: { voiceId: string; sourceId: string }) =>
      denoiseVoiceSource(input.voiceId, input.sourceId),
    onSuccess: () => {
      toast.success('Resemble wrote a child of this source')
      void client.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })

  return (
    <section className="flex min-h-0 flex-col gap-3">
      <header className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-semibold text-zinc-50">{source.title}</h3>
        <span className="text-xs text-zinc-400">
          {source.kind} · {source.duration_s.toFixed(1)}s · {source.clips.length} clip(s)
        </span>
      </header>

      <WaveformPeaks
        coverage={source.coverage}
        duration={source.duration_s}
        peaks={peaks.data ?? []}
        picked={pickedRange}
      />

      <div
        aria-label="analyzed coverage"
        aria-valuemax={1}
        aria-valuemin={0}
        aria-valuenow={fraction}
        className="relative h-1.5 w-full overflow-hidden rounded-full bg-zinc-700"
        data-coverage={fraction}
        role="progressbar"
      >
        <div className="h-full bg-emerald-400" style={{ width: `${Math.min(100, fraction * 100)}%` }} />
      </div>
      <p className="text-[11px] text-zinc-500">
        {covered.toFixed(1)}s / {source.duration_s.toFixed(1)}s analyzed
        {source.speakers.length > 0 ? ` · ${source.speakers.length} speaker(s)` : ''}
      </p>

      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-zinc-500">
          range from
          <input
            aria-label="range from"
            className="w-20 rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm normal-case text-zinc-100"
            inputMode="decimal"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-zinc-500">
          range to
          <input
            aria-label="range to"
            className="w-20 rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm normal-case text-zinc-100"
            inputMode="decimal"
            value={to}
            onChange={(event) => setTo(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="rounded-md border border-zinc-500 px-2.5 py-1 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!pickedRange || analyze.isPending}
          onClick={() => analyze.mutate({ start_s: pickedRange?.start_s ?? 0, end_s: pickedRange?.end_s ?? 0 })}
        >
          ANALYZE selection
        </button>
        <button
          type="button"
          className="rounded-md border border-zinc-500 px-2.5 py-1 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={analyze.isPending || source.duration_s <= 0}
          onClick={() => analyze.mutate({ start_s: 0, end_s: source.duration_s })}
        >
          ANALYZE visible
        </button>
        <button
          type="button"
          className="rounded-md border border-zinc-500 px-2.5 py-1 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={analyze.isPending || source.duration_s <= 0}
          onClick={() => analyze.mutate({ all: true })}
        >
          ANALYZE whole
        </button>
      </div>
      {issue ? <p className="text-xs text-amber-200">{issue}</p> : null}

      <div>
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">speaker lanes</h4>
        {source.speakers.length === 0 ? (
          <p className="mt-1 text-xs text-zinc-500">No speaker lanes yet.</p>
        ) : (
          <ul aria-label="speaker lanes" className="mt-1 flex flex-col gap-1">
            {source.speakers.map((speaker) => (
              <li className="flex items-center gap-2" key={speaker.local_id}>
                <span className="w-36 shrink-0 truncate text-[11px] text-zinc-400">
                  {speaker.label}
                  {speaker.mapped_voice_id ? (
                    <span className="text-emerald-400"> → {speaker.mapped_voice_id}</span>
                  ) : null}
                </span>
                {(voices.data?.length ?? 0) === 0 ? null : (
                  <select
                    aria-label={`${speaker.label} voice`}
                    className="w-32 shrink-0 rounded-md border border-zinc-600 bg-zinc-950 px-1 py-0.5 text-[11px] text-zinc-100"
                    disabled={map.isPending}
                    value={speaker.mapped_voice_id ?? ''}
                    onChange={(event) =>
                      map.mutate({ localId: speaker.local_id, voiceId: event.target.value || null })
                    }
                  >
                    <option value="">not mapped</option>
                    {(voices.data ?? []).map((voice) => (
                      <option key={voice.id} value={voice.id}>
                        {voice.name}
                      </option>
                    ))}
                  </select>
                )}
                <div className="relative h-7 min-w-0 flex-1 rounded bg-zinc-950">
                  {turns
                    .filter((turn) => turn.speaker_id === speaker.local_id)
                    .map((turn) => (
                      <button
                        type="button"
                        aria-pressed={picked.includes(turnKey(turn))}
                        className={cn(
                          'absolute top-0.5 h-6 overflow-hidden rounded-sm px-1 text-left text-[10px] leading-6 text-zinc-100',
                          picked.includes(turnKey(turn))
                            ? 'bg-emerald-500/40 ring-1 ring-emerald-300'
                            : 'bg-zinc-600',
                          turn.overlap
                            ? 'cursor-not-allowed bg-amber-500/20 text-amber-200 line-through'
                            : '',
                        )}
                        disabled={Boolean(turn.overlap)}
                        key={turnKey(turn)}
                        onClick={() =>
                          setPicked((current) =>
                            current.includes(turnKey(turn))
                              ? current.filter((key) => key !== turnKey(turn))
                              : [...current, turnKey(turn)],
                          )
                        }
                        style={{
                          left: `${(turn.start_s / Math.max(source.duration_s, 0.001)) * 100}%`,
                          width: `${((turn.end_s - turn.start_s) / Math.max(source.duration_s, 0.001)) * 100}%`,
                        }}
                      >
                        {`${turn.speaker_id} ${turn.start_s.toFixed(2)}s–${turn.end_s.toFixed(2)}s`}
                        {turn.overlap ? ' overlap' : ''}
                      </button>
                    ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>


      {mixedSpeakers ? (
        <p className="text-xs text-amber-200">
          One clip carries one speaker. Deselect the other speaker before extracting.
        </p>
      ) : null}

      <button
        type="button"
        className="w-fit rounded-md border border-zinc-500 px-3 py-1.5 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
        disabled={chosen.length === 0 || mixedSpeakers || extract.isPending}
        onClick={() => {
          setCapError(null)
          extract.mutate()
        }}
      >
        {chosen.length > 0 ? `Extract ${chosen.length} turn(s)` : 'Extract turns'}
      </button>
      {capError ? <p className="text-xs text-amber-200">{capError}</p> : null}

      {target ? (
        <div className="flex flex-col gap-2 rounded-md border border-zinc-700 bg-zinc-950 p-2">
          <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            enrolled source
          </h4>
          <label className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-wide text-zinc-500">
            Enrolled source
            <select
              aria-label="Enrolled source"
              className="min-w-0 flex-1 rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-xs normal-case text-zinc-100"
              value={target.source.id}
              onChange={(event) => setEnrolledKey(event.target.value)}
            >
              {enrolled.map((item) => (
                <option key={item.source.id} value={item.source.id}>
                  {item.voice.name} · {item.source.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-zinc-500">
            transcript
            <textarea
              aria-label="source transcript"
              className="min-h-16 w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm normal-case text-zinc-100"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onBlur={() => {
                if (draft !== (target.source.transcript ?? '')) {
                  saveTranscript.mutate({
                    voiceId: target.voice.id,
                    sourceId: target.source.id,
                    transcript: draft,
                  })
                }
              }}
            />
          </label>
          <p className="text-[11px] text-zinc-500">
            {target.source.transcript_locked
              ? 'Locked: transcription never overwrites this transcript.'
              : 'A manual edit locks this source’s transcript.'}
          </p>

          <div className="flex flex-wrap items-end gap-2">
            <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-zinc-500">
              Crop name
              <input
                aria-label="Crop name"
                className="w-36 rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-xs normal-case text-zinc-100"
                placeholder="tight crop"
                value={cropName}
                onChange={(event) => setCropName(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2.5 py-1 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
              disabled={ranges.length === 0 || crop.isPending}
              onClick={() =>
                crop.mutate({
                  voiceId: target.voice.id,
                  sourceId: target.source.id,
                  intervals: ranges,
                  name: cropName.trim(),
                })
              }
            >
              Create crop
            </button>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2.5 py-1 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
              disabled={!isPrimary || resemble.isPending}
              onClick={() => resemble.mutate({ voiceId: target.voice.id, sourceId: target.source.id })}
            >
              Resemble source
            </button>
          </div>
          {isPrimary ? null : (
            <p className="text-[11px] text-amber-300">
              Resemble runs on {target.voice.name}&rsquo;s primary source, not on this sibling.
            </p>
          )}

          <div>
            <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">lineage</h4>
            {lineage.length === 0 ? (
              <p className="mt-1 text-xs text-zinc-500">
                Nothing derived from this source yet. A crop or Resemble pass lands here.
              </p>
            ) : (
              <ul
                aria-label="source lineage"
                className="mt-1 flex flex-col gap-0.5 text-[11px] text-zinc-300"
              >
                {lineage.map((item) => {
                  const parent = lineage.find((other) => other.id === item.parent_id)
                  return (
                    <li key={item.id}>
                      {parent
                        ? `${artifactWord(parent)} → ${artifactWord(item)}`
                        : `${artifactWord(item)} (this source)`}
                    </li>
                  )
                })}
              </ul>
            )}
            <button
              type="button"
              className="mt-1 w-fit text-[11px] text-zinc-300 underline"
              onClick={() => selectVoice(target.voice.id)}
            >
              Show in voice store
            </button>
          </div>
        </div>
      ) : null}
    </section>
  )
}
