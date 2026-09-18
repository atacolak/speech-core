import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import {
  artifactAudioUrl,
  deleteRun,
  formatApiError,
  patchVoice,
  rateRun,
  renameRun,
} from '@/lib/api'
import type { RunItem, Voice } from '@/lib/api'
import { clampTakeLimit, DEFAULT_TAKE_LIMIT } from '@/lib/generation'
import { formatMs, formatSeconds } from '@/lib/format'
import { takeTranscript } from '@/lib/take-transcript'
import { cn } from '@/lib/utils'

export function TakeCard({
  voice,
  run,
  onSave,
  onDelete,
  onInspect,
  inspected,
}: {
  voice: Voice
  run: RunItem
  onSave: () => void
  onDelete: () => void
  onInspect: () => void
  inspected: boolean
}) {
  const snapshot = run.request_snapshot?.generation as
    | { seed?: number; guidance?: { cfg?: number; mode?: string; reference?: number; instruction?: number } }
    | undefined
  const saved = run.rating === 'keep'
  const guidance = snapshot?.guidance
  const transcript = takeTranscript(run)
  const client = useQueryClient()
  const [title, setTitle] = useState(run.name?.trim() ?? '')
  const rename = useMutation({
    mutationFn: (name: string) => renameRun(run.id, name),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['runs'] })
      void client.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })
  const commitTitle = () => {
    const next = title.trim()
    if (!next || next === (run.name?.trim() ?? '')) {
      setTitle(run.name?.trim() ?? '')
      return
    }
    rename.mutate(next)
  }
  const cfgLabel =
    guidance?.mode === 'dual'
      ? `dual ${guidance.reference ?? '—'}/${guidance.instruction ?? '—'}`
      : `cfg ${guidance?.cfg ?? '—'}`
  return (
    <li>
      <div
        className={cn(
          'rounded-md border p-3',
          saved ? 'border-zinc-100 bg-zinc-900' : 'border-zinc-600 bg-zinc-900',
        )}
      >
        <div className="w-full text-left">
          <input
            aria-label="Take title"
            className="w-full bg-transparent text-xs text-zinc-200 outline-none placeholder:text-zinc-500"
            onBlur={commitTitle}
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                commitTitle()
              }
            }}
            placeholder="Untitled"
            value={title}
          />
          <p className="text-xs text-zinc-300">
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
            <p className="mt-1 line-clamp-4 text-[11px] italic text-zinc-500">{transcript.text}</p>
          ) : null}
        </div>
        <audio className="mt-2 w-full" controls src={artifactAudioUrl(run.output_artifact_id)} />
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          <button type="button" className="text-zinc-200 underline" onClick={onSave}>
            {saved ? '☆ Saved' : '☆ Save'}
          </button>
          <a
            className="text-zinc-200 underline"
            href={artifactAudioUrl(run.output_artifact_id)}
            download={`${run.id}.wav`}
          >
            ↓ Download
          </a>
          <details>
            <summary className="cursor-pointer text-zinc-300">⋯</summary>
            <div className="mt-1 flex flex-col gap-1">
              <button type="button" onClick={onInspect}>
                Inspect provenance
              </button>
              <button type="button" onClick={onDelete}>
                Delete
              </button>
            </div>
          </details>
        </div>
        {inspected ? (
          <pre className="mt-2 max-h-40 overflow-auto rounded bg-zinc-950 p-2 text-[11px] text-zinc-400">
            {JSON.stringify(run.request_snapshot ?? run, null, 2)}
          </pre>
        ) : null}
      </div>
    </li>
  )
}

export function TakesSection({
  voice,
  takes,
  pending,
}: {
  voice: Voice | undefined
  takes: RunItem[]
  pending: boolean
}) {
  const client = useQueryClient()
  const [provenanceId, setProvenanceId] = useState<string | null>(null)
  const takeLimit = clampTakeLimit(voice?.take_limit ?? DEFAULT_TAKE_LIMIT)
  const rate = useMutation({
    mutationFn: ({ id, rating }: { id: string; rating: string }) => rateRun(id, rating),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runs'] }),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const remove = useMutation({
    mutationFn: deleteRun,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runs'] }),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const saveVoice = useMutation({
    mutationFn: ({ id, take_limit }: { id: string; take_limit: number }) =>
      patchVoice(id, { take_limit }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['voices'] }),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const setTakeLimit = (value: number) => {
    if (!voice) return
    const next = clampTakeLimit(value)
    if (next !== takeLimit) {
      saveVoice.mutate({ id: voice.id, take_limit: next })
    }
  }

  if (!voice) {
    return <p className="text-sm text-zinc-200">Select a voice to see its takes.</p>
  }

  const card = (run: RunItem) => (
    <TakeCard
      key={run.id}
      voice={voice}
      run={run}
      onSave={() => rate.mutate({ id: run.id, rating: run.rating === 'keep' ? '' : 'keep' })}
      onDelete={() => remove.mutate(run.id)}
      onInspect={() => setProvenanceId(run.id)}
      inspected={provenanceId === run.id}
    />
  )

  return (
    <div className="flex flex-col gap-4">
      <section role="group" aria-label="Latest take" className="space-y-2">
        <h2 className="text-base font-semibold text-zinc-50">Latest take</h2>
        {takes[0] ? <ul className="flex flex-col gap-2">{card(takes[0])}</ul> : <p>No takes yet.</p>}
      </section>
      <section role="group" aria-label="Takes" className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-base font-semibold text-zinc-50">Takes</h2>
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
        </div>
        <p className="text-[11px] text-zinc-500">
          {takes.slice(1).filter((run) => run.rating !== 'keep').length}/{takeLimit} unsaved ·{' '}
          {voice.name} only
        </p>
        {pending ? (
          <p className="text-sm text-zinc-200">Loading takes…</p>
        ) : takes.length <= 1 ? (
          <p className="text-sm text-zinc-200">No older takes.</p>
        ) : (
          <ul className="flex flex-col gap-2">{takes.slice(1).map(card)}</ul>
        )}
      </section>
    </div>
  )
}
