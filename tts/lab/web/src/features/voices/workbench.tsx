/**
 * The lab's voice workbench. A voice is a profile: sources in, references approved for Breeze.
 * The instrument in the middle is the waveform plus the keep crop that bounds the reference;
 * everything the operator selects shows its lineage on the right. AuK is parked: no processor
 * runs beside Breeze, and legacy experiments stay listed as artifacts only.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin from 'wavesurfer.js/dist/plugins/regions.esm.js'
import { AudioBar } from '@/components/audio-bar'
import { type VoiceAssets, voiceAssets } from '@/features/voices/voice-assets'
import {
  ApiError,
  activateVariant,
  addVoiceSource,
  approveAukCandidate,
  artifactAudioUrl,
  effectiveDurationS,
  excludeInterval,
  fetchRuntime,
  fetchVoices,
  formatApiError,
  isCroppedReference,
  keepOnly,
  loadE2,
  patchVoice,
  resetReference,
  sourceDurationS,
  transcribeVoice,
  unloadE2,
  type Interval,
  type RuntimeInfo,
  type Voice,
  type VoiceArtifact,
} from '@/lib/api'
import { formatSeconds } from '@/lib/format'
import { useWorkspace } from '@/state/workspace'

function complement(keep: Interval[], duration: number): Interval[] {
  const ordered = [...keep].sort((a, b) => a.start_s - b.start_s)
  const gaps: Interval[] = []
  let cursor = 0
  for (const interval of ordered) {
    if (interval.start_s > cursor + 1e-4) {
      gaps.push({ start_s: cursor, end_s: interval.start_s })
    }
    cursor = Math.max(cursor, interval.end_s)
  }
  if (duration > cursor + 1e-4) {
    gaps.push({ start_s: cursor, end_s: duration })
  }
  return gaps
}

/** Breeze XOR the parked mouth: whoever holds the 4070 decides what the workbench may load. */
function occupancyFor(info: RuntimeInfo | undefined): {
  busy: boolean
  breezeReady: boolean
  liveCall: boolean
  hint: string
} {
  const state = info?.state ?? 'unloaded'
  const liveCall = Boolean(info?.live_call_active)
  const busy = state === 'loading' || state === 'unloading' || Boolean(info?.processor)
  const breezeReady = state === 'ready'
  let hint =
    'GPU is free. Loading Breeze unloads any resident processor first; the parked mouth stays parked.'
  if (liveCall) {
    hint = 'A live call is using the mouth. Finish or stop the call before loading Breeze — it needs the GPU.'
  } else if (state === 'loading') {
    hint = 'Loading Breeze onto the GPU…'
  } else if (state === 'unloading') {
    hint = 'Unloading Breeze to free the GPU…'
  } else if (busy) {
    hint = 'An offline processor holds the GPU. Breeze stays unloaded until you load it again.'
  } else if (breezeReady) {
    hint = 'Breeze is on the GPU. Load it again when you want to clone.'
  }
  return { busy, breezeReady, liveCall, hint }
}

/** Exclusion paint is an overlay: never an operator selection. */
function isOverlayRegion(region: { id: string }): boolean {
  return String(region.id).startsWith('excl-')
}

/** Operator word for an asset: its name when the profile gave it one, else a neutral word. */
function assetWord(artifact: VoiceArtifact): string {
  return artifact.name ?? 'candidate'
}

function AssetList({
  label,
  empty,
  assetIds,
  word,
  comparison,
  marker,
  onPick,
}: {
  label: string
  empty: string
  assetIds: string[]
  word: (id: string) => string
  comparison: string[]
  marker?: (id: string) => string | null
  onPick: (id: string) => void
}) {
  return (
    <div className="mt-2">
      <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-400">{label}</h4>
      <ul aria-label={label} className="mt-1 grid gap-1">
        {assetIds.length === 0 ? (
          <li className="text-xs text-zinc-500">{empty}</li>
        ) : (
          assetIds.map((id) => (
            <li key={id} className="flex items-center gap-1 text-xs">
              <button
                type="button"
                aria-pressed={comparison.includes(id)}
                className="min-w-0 flex-1 truncate rounded-md border border-zinc-600 px-2 py-1 text-left text-zinc-200 aria-pressed:border-emerald-600"
                onClick={() => onPick(id)}
              >
                {word(id)}
              </button>
              {marker?.(id) ? <span className="text-amber-300">{marker(id)}</span> : null}
            </li>
          ))
        )}
      </ul>
    </div>
  )
}

export function VoiceWorkbench() {
  const client = useQueryClient()
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const closeEditor = useWorkspace((state) => state.closeEditor)
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const runtime = useQuery({
    queryKey: ['runtime'],
    queryFn: fetchRuntime,
    refetchInterval: (current) => {
      const state = current.state.data?.state
      return state === 'loading' || state === 'unloading' ? 700 : 2500
    },
  })
  const voice = voices.data?.find((item) => item.id === selectedVoiceId)
  const host = useRef<HTMLDivElement>(null)
  const wave = useRef<WaveSurfer | null>(null)
  const regionsRef = useRef<RegionsPlugin | null>(null)
  const [sourceId, setSourceId] = useState<string | null>(null)
  const [pickedId, setPickedId] = useState<string | null>(null)
  const [comparison, setComparison] = useState<string[]>([])
  const [selection, setSelection] = useState<Interval | null>(null)
  const [undo, setUndo] = useState<Interval[][]>([])
  const [transcript, setTranscript] = useState('')

  const assets: VoiceAssets = voiceAssets(voice)
  const primarySource = assets.sources.find((item) => item.id === assets.primarySourceId)
  const source = assets.sources.find((item) => item.id === sourceId) ?? primarySource
  /** Keep-only, exclude and the transcript write to the primary source; t1 owns per-source crops. */
  const cropTargetsPrimary = Boolean(primarySource) && source?.id === primarySource?.id
  const keep = source?.keep_intervals?.length ? source.keep_intervals : (voice?.keep_intervals ?? [])
  const keepKey = JSON.stringify(keep)
  const keepRef = useRef<Interval[]>(keep)
  keepRef.current = keep
  const artifacts = [...assets.references, ...assets.experiments]
  const inspected =
    artifacts.find((item) => item.id === pickedId) ??
    assets.references.find((item) => item.id === assets.defaultReferenceId) ??
    null
  const compared = comparison.flatMap((id) => {
    const artifact = artifacts.find((item) => item.id === id)
    return artifact ? [artifact] : []
  })
  const artifactWords = new Map(artifacts.map((item) => [item.id, assetWord(item)]))
  const sourceWords = new Map(assets.sources.map((item) => [item.id, item.label]))

  useEffect(() => {
    setTranscript(
      source?.transcript ?? voice?.effective_transcript ?? voice?.source_transcript ?? '',
    )
  }, [voice?.id, source?.id, source?.transcript, voice?.effective_transcript, voice?.source_transcript])

  const sourceArtifactId = source?.artifact_id

  const paintExclusions = () => {
    const instance = wave.current
    const regions = regionsRef.current
    if (!instance || !regions) {
      return
    }
    const duration = instance.getDuration() || voice?.duration_s || 0
    for (const existing of regions.getRegions()) {
      if (isOverlayRegion(existing)) {
        existing.remove()
      }
    }
    complement(keepRef.current, duration).forEach((gap, index) => {
      regions.addRegion({
        id: `excl-${index}`,
        start: gap.start_s,
        end: gap.end_s,
        color: 'rgba(127, 29, 29, 0.35)',
        drag: false,
        resize: false,
      })
    })
  }

  useEffect(() => {
    const container = host.current
    if (!container || !sourceArtifactId) {
      return
    }
    const regions = RegionsPlugin.create()
    regionsRef.current = regions
    const instance = WaveSurfer.create({
      container,
      url: artifactAudioUrl(sourceArtifactId),
      height: 96,
      waveColor: '#a1a1aa',
      progressColor: '#fafafa',
      cursorColor: '#e4e4e7',
      plugins: [regions],
    })
    wave.current = instance
    regions.enableDragSelection({ color: 'rgba(16, 185, 129, 0.25)' })
    regions.on('region-created', (region) => {
      if (isOverlayRegion(region)) {
        return
      }
      for (const other of regions.getRegions()) {
        if (other.id !== region.id && !isOverlayRegion(other)) {
          other.remove()
        }
      }
      setSelection({ start_s: region.start, end_s: region.end })
    })
    regions.on('region-updated', (region) => {
      if (isOverlayRegion(region)) {
        return
      }
      setSelection({ start_s: region.start, end_s: region.end })
    })
    instance.on('ready', () => {
      paintExclusions()
    })
    instance.on('decode', () => {
      paintExclusions()
    })
    return () => {
      instance.destroy()
      wave.current = null
      regionsRef.current = null
    }
  }, [voice?.id, sourceArtifactId])

  useEffect(() => {
    paintExclusions()
  }, [keepKey, sourceArtifactId])

  const invalidate = (next?: Voice) => {
    void client.invalidateQueries({ queryKey: ['voices'] })
    return next
  }

  const mutateKeep = useMutation<Voice, Error, Interval>({
    mutationFn: (interval) => keepOnly(voice!.id, interval),
    onSuccess: invalidate,
    onError: (error) => toast.error(formatApiError(error)),
  })
  const mutateExclude = useMutation<Voice, Error, Interval>({
    mutationFn: (interval) => excludeInterval(voice!.id, interval),
    onSuccess: invalidate,
    onError: (error) => toast.error(formatApiError(error)),
  })
  const mutateReset = useMutation({
    mutationFn: resetReference,
    onSuccess: invalidate,
    onError: (error) => toast.error(formatApiError(error)),
  })
  const saveTranscript = useMutation({
    mutationFn: (value: string) =>
      patchVoice(voice!.id, { effective_transcript: value, source_transcript: value }),
    onSuccess: invalidate,
  })
  const transcribe = useMutation({
    mutationFn: transcribeVoice,
    onSuccess: invalidate,
    onError: (error) => toast.error(formatApiError(error)),
  })
  /** The multi-source route lands with the profile schema; until then this fails closed. */
  const addAudio = useMutation<Voice, Error, File>({
    mutationFn: (audio) => addVoiceSource(voice!.id, { label: audio.name, audio }),
    onSuccess: (body) => {
      void client.invalidateQueries({ queryKey: ['voices'] })
      const added = (body.sources ?? []).at(-1)
      if (added) {
        setSourceId(added.id)
      }
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError && (error.status === 404 || error.status === 501)
          ? 'Multi-source audio is not available in this lab build yet — the profile keeps one source.'
          : formatApiError(error),
      )
    },
  })
  const approveCandidate = useMutation<Voice, Error, string>({
    mutationFn: async (variantId) => {
      await approveAukCandidate(voice!.id, variantId)
      return activateVariant(voice!.id, variantId)
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['voices'] })
      toast.success('Candidate approved and set as the Breeze reference.')
    },
    onError: (error) => toast.error(formatApiError(error)),
  })
  const occupancy = occupancyFor(runtime.data)
  const loadBreeze = useMutation({
    mutationFn: loadE2,
    onSuccess: (body) => {
      void client.invalidateQueries({ queryKey: ['runtime'] })
      if (body.state === 'insufficient_vram' || body.state === 'error') {
        toast.error(formatApiError(body.last_error?.message || body.state))
      }
    },
    onError: (error) => toast.error(formatApiError(error)),
  })
  const unloadBreeze = useMutation({
    mutationFn: unloadE2,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtime'] }),
    onError: (error) => toast.error(formatApiError(error)),
  })

  if (!voice) {
    return null
  }

  const pushUndo = () => setUndo((stack) => [...stack, keepRef.current])
  const duration = sourceDurationS(voice)
  const keepSeconds = effectiveDurationS(voice)
  const cropped = isCroppedReference(voice)
  const pickAsset = (id: string) => {
    setPickedId(id)
    setComparison((ids) => (ids.includes(id) ? ids : [...ids, id]))
  }

  return (
    <section
      aria-label="voice workbench"
      className="flex min-h-0 flex-1 flex-col overflow-hidden bg-zinc-900 p-4 text-zinc-100"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold">Voice workbench · {voice.name}</h2>
        <span className="text-xs text-zinc-300">{occupancy.hint}</span>
        <span className="rounded border border-zinc-600 px-2 py-0.5 text-[11px] uppercase tracking-wide">
          {occupancy.breezeReady ? 'Breeze' : 'GPU free'}
        </span>
        {occupancy.breezeReady ? (
          <button
            type="button"
            className="rounded-md border border-zinc-500 px-2 py-1 text-xs"
            disabled={unloadBreeze.isPending || occupancy.liveCall}
            onClick={() => unloadBreeze.mutate()}
          >
            {unloadBreeze.isPending ? 'Unloading Breeze…' : 'Unload Breeze'}
          </button>
        ) : (
          <button
            type="button"
            className="rounded-md border border-zinc-500 px-2 py-1 text-xs"
            disabled={loadBreeze.isPending || occupancy.busy || occupancy.liveCall}
            onClick={() => loadBreeze.mutate()}
          >
            {loadBreeze.isPending ? 'Loading Breeze…' : 'Load Breeze'}
          </button>
        )}
        <button
          type="button"
          className="ml-auto rounded-md border border-zinc-500 px-3 py-1.5 text-sm"
          onClick={() => closeEditor()}
        >
          Done
        </button>
      </div>
      <div className="mt-3 grid min-h-0 flex-1 grid-cols-[15rem_minmax(0,1fr)_17rem] gap-3 overflow-hidden">
        <aside
          aria-label="assets"
          className="overflow-y-auto rounded-md border border-zinc-700 bg-zinc-950 p-2"
        >
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">assets</h3>
            <label className="cursor-pointer text-[11px] text-zinc-300 underline">
              add audio
              <input
                type="file"
                accept="audio/*"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  if (file) {
                    addAudio.mutate(file)
                  }
                }}
              />
            </label>
          </div>
          <AssetList
            label="sources"
            empty="No sources yet."
            assetIds={assets.sources.map((item) => item.id)}
            word={(id) => sourceWords.get(id) ?? id}
            comparison={[]}
            onPick={(id) => setSourceId(id)}
          />
          <AssetList
            label="references"
            empty="No references yet. Approve an experiment to make one."
            assetIds={assets.references.map((item) => item.id)}
            word={(id) => artifactWords.get(id) ?? id}
            comparison={comparison}
            marker={(id) => (id === assets.defaultReferenceId ? '★' : null)}
            onPick={pickAsset}
          />
          <AssetList
            label="experiments"
            empty="No experiments yet. Generate one from the workbench."
            assetIds={assets.experiments.map((item) => item.id)}
            word={(id) => artifactWords.get(id) ?? id}
            comparison={comparison}
            marker={(id) => {
              const artifact = assets.experiments.find((item) => item.id === id)
              return artifact?.stale ? 'stale' : null
            }}
            onPick={pickAsset}
          />
        </aside>
        <div className="min-h-0 overflow-y-auto">
          <div ref={host} className="rounded-md border border-zinc-700 bg-zinc-950 p-2" />
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-zinc-300">
            {selection ? (
              <span>
                {selection.start_s.toFixed(2)}s → {selection.end_s.toFixed(2)}s
              </span>
            ) : (
              <span className="text-zinc-500">Drag on the waveform to select</span>
            )}
            <span className="text-[11px] text-zinc-400">
              keep {formatSeconds(keepSeconds)}
              {cropped ? ` · source ${formatSeconds(duration)}` : ''}
            </span>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2 py-1"
              disabled={!selection || !cropTargetsPrimary}
              onClick={() => wave.current?.play(selection?.start_s, selection?.end_s)}
            >
              ▶ play selection
            </button>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2 py-1"
              disabled={!selection || !cropTargetsPrimary}
              onClick={() => {
                if (!selection) return
                pushUndo()
                mutateKeep.mutate(selection)
              }}
            >
              Keep only
            </button>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2 py-1"
              disabled={!selection || !cropTargetsPrimary}
              onClick={() => {
                if (!selection) return
                pushUndo()
                mutateExclude.mutate(selection, { onSuccess: () => setSelection(null) })
              }}
            >
              Exclude
            </button>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2 py-1"
              onClick={() => setSelection(null)}
            >
              Clear
            </button>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2 py-1"
              disabled={undo.length === 0}
              onClick={() => {
                const previous = undo[undo.length - 1]
                if (!previous) return
                setUndo(undo.slice(0, -1))
                void patchVoice(voice.id, { keep_intervals: previous }).then(invalidate)
              }}
            >
              Undo
            </button>
            <button
              type="button"
              className="rounded-md border border-zinc-500 px-2 py-1"
              disabled={!cropTargetsPrimary}
              onClick={() => {
                pushUndo()
                mutateReset.mutate(voice.id)
              }}
            >
              Reset to original
            </button>
          </div>
          {cropTargetsPrimary ? null : (
            <p className="mt-1 text-[11px] text-amber-300">
              Cropping and the transcript write the primary source. Pick{' '}
              {primarySource?.label ?? 'the primary source'} to edit them — per-source crops land
              with the profile schema.
            </p>
          )}
          <label className="mt-3 block text-[11px] font-medium uppercase tracking-wide text-zinc-400">
            transcript
            <textarea
              className="mt-1 min-h-16 w-full rounded-md border border-zinc-600 bg-zinc-950 px-3 py-2 text-sm text-zinc-100"
              value={transcript}
              disabled={!cropTargetsPrimary}
              onChange={(event) => setTranscript(event.target.value)}
              onBlur={() => {
                if (transcript !== (voice.effective_transcript || voice.source_transcript)) {
                  saveTranscript.mutate(transcript)
                }
              }}
            />
          </label>
          <button
            type="button"
            className="mt-1 w-fit text-xs text-zinc-300 underline"
            disabled={transcribe.isPending || !cropTargetsPrimary}
            onClick={() => transcribe.mutate(voice.id)}
          >
            {transcribe.isPending ? 'Transcribing…' : 'Transcribe from audio'}
          </button>
        </div>
        <aside
          aria-label="inspector"
          className="overflow-y-auto rounded-md border border-zinc-700 bg-zinc-950 p-2"
        >
          <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">inspector</h3>
          {inspected ? (
            <dl className="mt-2 grid gap-1 text-xs">
              <dt className="text-[11px] uppercase tracking-wide text-zinc-500">asset</dt>
              <dd className="text-zinc-200">{assetWord(inspected)}</dd>
              <dt className="text-[11px] uppercase tracking-wide text-zinc-500">role</dt>
              <dd className="text-zinc-200">{inspected.role}</dd>
              {inspected.auk_task ? (
                <>
                  <dt className="text-[11px] uppercase tracking-wide text-zinc-500">task</dt>
                  <dd className="text-zinc-200">{inspected.auk_task}</dd>
                </>
              ) : null}
              {inspected.instruction ? (
                <>
                  <dt className="text-[11px] uppercase tracking-wide text-zinc-500">instruction</dt>
                  <dd className="text-zinc-400">{inspected.instruction}</dd>
                </>
              ) : null}
              <dt className="text-[11px] uppercase tracking-wide text-zinc-500">parent</dt>
              <dd className="text-zinc-400">{inspected.parent_id ?? '—'}</dd>
              <dt className="text-[11px] uppercase tracking-wide text-zinc-500">source</dt>
              <dd className="text-zinc-400">{inspected.source_id ?? '—'}</dd>
              {inspected.seed == null ? null : (
                <>
                  <dt className="text-[11px] uppercase tracking-wide text-zinc-500">seed</dt>
                  <dd className="text-zinc-400">seed {inspected.seed}</dd>
                </>
              )}
              {inspected.auk_precision ? (
                <>
                  <dt className="text-[11px] uppercase tracking-wide text-zinc-500">precision</dt>
                  <dd className="text-zinc-400">{inspected.auk_precision}</dd>
                </>
              ) : null}
              <dt className="text-[11px] uppercase tracking-wide text-zinc-500">status</dt>
              <dd className="text-zinc-400">
                {[inspected.approved ? 'approved' : null, inspected.stale ? 'stale' : null]
                  .filter(Boolean)
                  .join(' · ') || 'candidate'}
              </dd>
            </dl>
          ) : (
            <p className="mt-2 text-xs text-zinc-500">
              Pick an asset to read the task, instruction and parent that made it.
            </p>
          )}
        </aside>
      </div>
      <section
        aria-label="compare"
        className="mt-3 rounded-md border border-zinc-700 bg-zinc-950 p-2"
      >
        <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">compare</h3>
        {compared.length === 0 ? (
          <p className="mt-1 text-xs text-zinc-500">
            Select an experiment to compare against the source.
          </p>
        ) : null}
        <div className="mt-2 grid gap-2">
          <AudioBar
            label={source?.label ? `source · ${source.label}` : 'source'}
            src={source ? artifactAudioUrl(source.artifact_id) : undefined}
          />
          {compared.map((artifact) => (
            <div key={artifact.id} className="rounded-md border border-zinc-700 p-2">
              <AudioBar
                label={assetWord(artifact)}
                src={artifactAudioUrl(artifact.audio_artifact_id)}
              />
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
                <span className="text-zinc-400">
                  {artifact.role}
                  {artifact.auk_precision ? ` · ${artifact.auk_precision}` : ''}
                  {artifact.seed == null ? '' : ` · seed ${artifact.seed}`}
                </span>
                {artifact.stale ? (
                  <span className="rounded border border-amber-700 px-1 text-[10px] uppercase text-amber-300">
                    stale
                  </span>
                ) : null}
                <button
                  type="button"
                  className="rounded-md border border-zinc-500 px-2 py-1"
                  onClick={() => setComparison((ids) => ids.filter((id) => id !== artifact.id))}
                >
                  Remove {assetWord(artifact)} from compare
                </button>
                {artifact.role === 'experiment' ? (
                  <button
                    type="button"
                    className="ml-auto rounded-md border border-zinc-500 px-2 py-1"
                    disabled={approveCandidate.isPending}
                    onClick={() => approveCandidate.mutate(artifact.id)}
                  >
                    Approve
                  </button>
                ) : null}
              </div>
            </div>
          ))}
        </div>
        <p className="mt-2 text-[11px] text-zinc-500">
          Approving promotes an experiment to the reference Breeze clones from. The others stay
          listed — nothing is deleted for you.
        </p>
      </section>
    </section>
  )
}
