import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { AudioBar } from '@/components/audio-bar'
import {
  activeReferenceLabel,
  artifactAudioUrl,
  deleteRun,
  fetchRuns,
  fetchVoices,
  formatApiError,
  patchVoice,
  rateRun,
} from '@/lib/api'
import {
  clampTakeLimit,
  DEFAULT_TAKE_LIMIT,
  fromStoredGeneration,
  toGenerationBody,
} from '@/lib/generation'
import { formatMs, formatSeconds } from '@/lib/format'
import { takeTranscript } from '@/lib/take-transcript'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/state/workspace'

function NumberSlider({
  label,
  value,
  min,
  max,
  step,
  disabled,
  onChange,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  disabled?: boolean
  onChange: (value: number) => void
}) {
  return (
    <label className={cn('block text-xs uppercase tracking-wide text-zinc-400', disabled && 'opacity-40')}>
      {label}
      <span className="mt-1 flex items-center gap-2">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          className="min-w-0 flex-1 disabled:cursor-not-allowed"
          value={value}
          onChange={(event) => onChange(Number(event.target.value))}
        />
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          className="w-16 rounded-md border border-zinc-600 bg-zinc-950 px-1.5 py-1 text-right text-sm normal-case text-zinc-100 disabled:cursor-not-allowed"
          value={Number.isFinite(value) ? value : ''}
          onChange={(event) => {
            const next = Number(event.target.value)
            if (!Number.isNaN(next)) {
              onChange(next)
            }
          }}
        />
      </span>
    </label>
  )
}

export function InspectorPane() {
  const client = useQueryClient()
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const selectedRunId = useWorkspace((state) => state.selectedRunId)
  const selectRun = useWorkspace((state) => state.selectRun)
  const generation = useWorkspace((state) => state.generation)
  const patchGeneration = useWorkspace((state) => state.patchGeneration)
  const replaceGeneration = useWorkspace((state) => state.replaceGeneration)
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const runs = useQuery({
    queryKey: ['runs', selectedVoiceId],
    queryFn: () => fetchRuns(selectedVoiceId),
    refetchInterval: 4000,
    enabled: Boolean(selectedVoiceId),
  })
  const voice = voices.data?.find((item) => item.id === selectedVoiceId)
  const hydratedId = useRef<string | null>(null)
  const [provenanceId, setProvenanceId] = useState<string | null>(null)
  const rate = useMutation({
    mutationFn: ({ id, rating }: { id: string; rating: string }) => rateRun(id, rating),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runs'] }),
  })
  const remove = useMutation({
    mutationFn: deleteRun,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runs'] }),
  })
  const saveVoice = useMutation({
    mutationFn: ({
      id,
      generation,
      take_limit,
    }: {
      id: string
      generation?: ReturnType<typeof toGenerationBody>
      take_limit?: number
    }) => patchVoice(id, { generation, take_limit }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['voices'] }),
    onError: (error) => toast.error(formatApiError(error)),
  })

  useEffect(() => {
    if (!voice) {
      hydratedId.current = null
      return
    }
    if (hydratedId.current === voice.id) {
      return
    }
    hydratedId.current = voice.id
    replaceGeneration(fromStoredGeneration(voice.generation))
  }, [voice, replaceGeneration])

  useEffect(() => {
    if (!voice || hydratedId.current !== voice.id) {
      return
    }
    const timer = window.setTimeout(() => {
      saveVoice.mutate({ id: voice.id, generation: toGenerationBody(generation) })
    }, 500)
    return () => window.clearTimeout(timer)
    // persist customized generation for the selected voice
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generation, voice?.id])

  const takeLimit = clampTakeLimit(voice?.take_limit ?? DEFAULT_TAKE_LIMIT)
  const takes = (runs.data ?? []).filter((run) => run.voice_id === selectedVoiceId)
  const unsaved = takes.filter((run) => run.rating !== 'keep')
  const setTakeLimit = (value: number) => {
    if (!voice) {
      return
    }
    const next = clampTakeLimit(value)
    if (next === takeLimit) {
      return
    }
    saveVoice.mutate({ id: voice.id, take_limit: next })
  }

  return (
    <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto bg-zinc-800 p-4">
      <h2 className="text-base font-semibold text-zinc-50">Settings</h2>
      <div className="space-y-3 text-sm">
        <div>
          <p className="text-xs uppercase tracking-wide text-zinc-400">Voice</p>
          <p className="text-zinc-100">{voice?.name ?? '—'}</p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-wide text-zinc-400">Reference</p>
          <p className="text-zinc-100">
            {activeReferenceLabel(voice)}
            {voice?.duration_s != null ? ` · ${formatSeconds(voice.duration_s)}` : ''}
          </p>
        </div>
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-zinc-400">Guidance</p>
          <label className="flex items-center gap-2 text-xs text-zinc-200">
            <input
              type="checkbox"
              checked={generation.dual}
              onChange={(event) => patchGeneration({ dual: event.target.checked })}
            />
            experimental dual-cfg
          </label>
          <NumberSlider
            label="cfg"
            value={generation.cfg}
            min={1}
            max={8}
            step={0.1}
            disabled={generation.dual}
            onChange={(value) => patchGeneration({ cfg: value })}
          />
          {generation.dual ? (
            <div className="space-y-2">
              <p className="normal-case tracking-normal text-[11px] text-zinc-500">
                offline 3-branch generate · first audio is the whole take
              </p>
              <NumberSlider
                label="reference"
                value={generation.cfgRef}
                min={0}
                max={8}
                step={0.1}
                onChange={(value) => patchGeneration({ cfgRef: value })}
              />
              <NumberSlider
                label="instruction"
                value={generation.cfgIns}
                min={0}
                max={8}
                step={0.1}
                onChange={(value) => patchGeneration({ cfgIns: value })}
              />
            </div>
          ) : null}
        </div>
        <label className="block text-xs uppercase tracking-wide text-zinc-400">
          Seed
          <span className="mt-1 flex items-center gap-2">
            <input
              type="number"
              className="w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm text-zinc-100"
              value={generation.seed}
              onChange={(event) => patchGeneration({ seed: Number(event.target.value) })}
            />
            <button
              type="button"
              className="text-zinc-300"
              onClick={() => patchGeneration({ seed: Math.floor(Math.random() * 10_000) })}
            >
              ↻
            </button>
          </span>
        </label>
        <details>
          <summary className="cursor-pointer text-sm text-zinc-200">Advanced generation</summary>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-zinc-400">
            {(
              [
                ['temperature', 'temperature'],
                ['depthTemperature', 'depth_temperature'],
                ['topK', 'top_k'],
                ['topP', 'top_p'],
                ['maxNewTokens', 'max_new_tokens'],
              ] as const
            ).map(([key, label]) => (
              <label key={key}>
                {label}
                <input
                  type="number"
                  step="0.1"
                  className="mt-1 w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm text-zinc-100"
                  value={generation[key]}
                  onChange={(event) => patchGeneration({ [key]: Number(event.target.value) })}
                />
              </label>
            ))}
            <label className="col-span-2 flex items-center gap-2 text-zinc-200">
              <input
                type="checkbox"
                checked={generation.doSample}
                onChange={(event) => patchGeneration({ doSample: event.target.checked })}
              />
              do_sample
            </label>
          </div>
        </details>
        <details>
          <summary className="cursor-pointer text-sm text-zinc-200">Pronunciation 0</summary>
          <p className="mt-2 text-xs text-zinc-400">
            Canonical utterance only. Synthesis text is derived, not a second field.
          </p>
        </details>
      </div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-zinc-50">Takes</h2>
        {voice ? (
          <label className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-zinc-400">
            cap
            <button
              type="button"
              className="rounded border border-zinc-500 px-1.5 py-0.5 text-zinc-200"
              onClick={() => setTakeLimit(takeLimit - 1)}
            >
              −
            </button>
            <input
              type="number"
              min={1}
              max={50}
              className="w-12 rounded-md border border-zinc-600 bg-zinc-950 px-1 py-0.5 text-center text-sm normal-case text-zinc-100"
              value={takeLimit}
              onChange={(event) => setTakeLimit(Number(event.target.value))}
            />
            <button
              type="button"
              className="rounded border border-zinc-500 px-1.5 py-0.5 text-zinc-200"
              onClick={() => setTakeLimit(takeLimit + 1)}
            >
              +
            </button>
          </label>
        ) : null}
      </div>
      {voice ? (
        <p className="text-[11px] text-zinc-500">
          {unsaved.length}/{takeLimit} unsaved · {voice.name} only
        </p>
      ) : null}
      {!voice ? (
        <p className="text-sm text-zinc-200">Select a voice to see its takes.</p>
      ) : runs.isPending ? (
        <p className="text-sm text-zinc-200">Loading takes…</p>
      ) : takes.length === 0 ? (
        <p className="text-sm text-zinc-200">No takes for {voice.name} yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {takes.map((run) => {
            const snapshot = run.request_snapshot?.generation as
              | { seed?: number; guidance?: { cfg?: number; mode?: string; reference?: number; instruction?: number } }
              | undefined
            const saved = run.rating === 'keep'
            const guidance = snapshot?.guidance
            const transcript = takeTranscript(run)
            const cfgLabel =
              guidance?.mode === 'dual'
                ? `dual ${guidance.reference ?? '—'}/${guidance.instruction ?? '—'}`
                : `cfg ${guidance?.cfg ?? '—'}`
            return (
              <li key={run.id}>
                <div
                  className={cn(
                    'rounded-md border p-3',
                    selectedRunId === run.id
                      ? 'border-emerald-400 bg-zinc-700'
                      : saved
                        ? 'border-zinc-100 bg-zinc-900'
                        : 'border-zinc-600 bg-zinc-900',
                  )}
                >
                  <button type="button" className="w-full text-left" onClick={() => selectRun(run.id)}>
                    <AudioBar label="" src={artifactAudioUrl(run.output_artifact_id)} />
                    <p className="mt-1 text-xs text-zinc-300">
                      {voice.name}
                      {saved ? ' · saved' : ''}
                    </p>
                    <p className="text-xs text-zinc-400">
                      {cfgLabel} · seed {snapshot?.seed ?? '—'} ·{' '}
                      {run.first_audio_ms != null
                        ? `first audio ${formatMs(run.first_audio_ms)}`
                        : formatSeconds(run.duration_s)}
                    </p>
                    {transcript.text ? (
                      <p className="mt-1 line-clamp-4 text-[11px] italic text-zinc-500">
                        {transcript.text}
                      </p>
                    ) : null}
                  </button>
                  <div className="mt-2 flex flex-wrap gap-2 text-xs">
                    <button
                      type="button"
                      className="text-zinc-200 underline"
                      onClick={() => rate.mutate({ id: run.id, rating: saved ? '' : 'keep' })}
                    >
                      {saved ? '☆ Saved' : '☆ Save'}
                    </button>
                    <a
                      className="text-zinc-200 underline"
                      href={artifactAudioUrl(run.output_artifact_id)}
                      download
                    >
                      ↓
                    </a>
                    <details>
                      <summary className="cursor-pointer text-zinc-300">⋯</summary>
                      <div className="mt-1 flex flex-col gap-1">
                        <button type="button" onClick={() => setProvenanceId(run.id)}>
                          Inspect provenance
                        </button>
                        <button type="button" onClick={() => remove.mutate(run.id)}>
                          Delete
                        </button>
                      </div>
                    </details>
                  </div>
                  {provenanceId === run.id ? (
                    <pre className="mt-2 max-h-40 overflow-auto rounded bg-zinc-950 p-2 text-[11px] text-zinc-400">
                      {JSON.stringify(run.request_snapshot ?? run, null, 2)}
                    </pre>
                  ) : null}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
