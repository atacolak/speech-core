import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { AudioBar } from '@/components/audio-bar'
import {
  activeReferenceLabel,
  artifactAudioUrl,
  createVoice,
  deleteVoice,
  effectiveDurationS,
  fetchRuntime,
  fetchVoices,
  formatApiError,
  isCroppedReference,
  isSampleVoice,
  patchVoice,
  pickDefaultVoice,
  setActiveVoice,
  sourceDurationS,
  voiceReferenceAudioUrl,
} from '@/lib/api'
import { formatSeconds } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/state/workspace'

const ACCEPT = '.wav,.mp3,.flac,.m4a,.ogg,.aac,.opus,audio/*'

export function VoicesPane() {
  const client = useQueryClient()
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const selectVoice = useWorkspace((state) => state.selectVoice)
  const openEditor = useWorkspace((state) => state.openEditor)
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: fetchRuntime })
  const [query, setQuery] = useState('')
  const [nameDraft, setNameDraft] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  const create = useMutation({
    mutationFn: createVoice,
    onSuccess: (voice) => {
      selectVoice(voice.id)
      void setActiveVoice(voice.id)
      toast.success(`Imported ${voice.name}`)
      void client.invalidateQueries({ queryKey: ['voices'] })
      void client.invalidateQueries({ queryKey: ['runtime'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })
  const rename = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => patchVoice(id, { name }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['voices'] }),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const remove = useMutation({
    mutationFn: deleteVoice,
    onSuccess: (_, id) => {
      if (selectedVoiceId === id) {
        const rest = (voices.data ?? []).filter((voice) => voice.id !== id)
        const next = pickDefaultVoice(rest)?.id ?? null
        selectVoice(next)
        void setActiveVoice(next)
      }
      toast.success('Voice deleted')
      void client.invalidateQueries({ queryKey: ['voices'] })
      void client.invalidateQueries({ queryKey: ['runs'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })

  useEffect(() => {
    if (selectedVoiceId) {
      return
    }
    const items = voices.data ?? []
    const active = runtime.data?.active_voice_id
    const fromTalker = items.find((voice) => voice.id === active)
    const picked = fromTalker ?? pickDefaultVoice(items)
    if (!picked) {
      return
    }
    selectVoice(picked.id)
    if (!fromTalker) {
      void setActiveVoice(picked.id)
    }
  }, [selectedVoiceId, voices.data, runtime.data?.active_voice_id, selectVoice])

  const visible = useMemo(() => {
    const items = voices.data ?? []
    const needle = query.trim().toLowerCase()
    const filtered = needle.length === 0
      ? items
      : items.filter(
          (voice) =>
            voice.name.toLowerCase().includes(needle) ||
            (voice.effective_transcript || voice.source_transcript).toLowerCase().includes(needle),
        )
    return [...filtered].sort((left, right) => Number(isSampleVoice(right)) - Number(isSampleVoice(left)))
  }, [voices.data, query])

  const selected = voices.data?.find((voice) => voice.id === selectedVoiceId)
  useEffect(() => {
    setNameDraft(selected?.name ?? '')
  }, [selected?.id, selected?.name])

  const variant = selected?.active_variant
  const hasReference = Boolean(selected?.source_audio_artifact_id && (selected.duration_s ?? 0) > 0)

  const commitName = () => {
    if (!selected) {
      return
    }
    const next = nameDraft.trim()
    if (!next || next === selected.name) {
      setNameDraft(selected.name)
      return
    }
    rename.mutate({ id: selected.id, name: next })
  }

  return (
    <section className="flex h-full min-h-0 flex-col gap-3 overflow-auto bg-zinc-800 p-4">
      <h2 className="text-base font-semibold text-zinc-50">Voices</h2>
      <input
        className="w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
        placeholder="Search voices"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {voices.isError ? (
        <p className="text-sm text-red-400">Could not load voices.</p>
      ) : voices.data && voices.data.length === 0 ? (
        <p className="rounded-md border border-zinc-600 bg-zinc-900 px-3 py-2 text-sm text-zinc-100">
          No voices yet.
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {visible.map((voice) => {
            const kind = activeReferenceLabel(voice)
            const quote = voice.effective_transcript || voice.source_transcript
            return (
              <li key={voice.id}>
                <button
                  type="button"
                  className={cn(
                    'w-full rounded-md border px-3 py-2 text-left',
                    selectedVoiceId === voice.id
                      ? 'border-emerald-400 bg-zinc-700'
                      : 'border-zinc-600 bg-zinc-900',
                  )}
                  onClick={() => {
                    selectVoice(voice.id)
                    void setActiveVoice(voice.id)
                  }}
                >
                  <span className="block text-sm font-medium text-zinc-50">
                    {voice.name}
                    {isSampleVoice(voice) ? (
                      <span className="ml-2 text-[10px] font-normal uppercase tracking-wide text-emerald-400">
                        sample
                      </span>
                    ) : null}
                  </span>
                  <span className="block text-xs text-zinc-400">
                    ▶ {kind} · {formatSeconds(effectiveDurationS(voice))}
                    {isCroppedReference(voice) ? (
                      <span className="text-zinc-500"> of {formatSeconds(sourceDurationS(voice))}</span>
                    ) : null}
                  </span>
                  {quote ? (
                    <span className="mt-0.5 block truncate text-xs italic text-zinc-500">
                      {quote}
                    </span>
                  ) : null}
                </button>
              </li>
            )
          })}
        </ul>
      )}
      {selected ? (
        <div className="flex flex-col gap-2 rounded-md border border-zinc-600 bg-zinc-900 p-3">
          <label className="text-xs uppercase tracking-wide text-zinc-400">
            Name
            <input
              className="mt-1 w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1.5 text-sm font-medium text-zinc-50"
              value={nameDraft}
              onChange={(event) => setNameDraft(event.target.value)}
              onBlur={commitName}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.currentTarget.blur()
                }
              }}
            />
          </label>
          {hasReference ? (
            <>
              <p className="text-xs text-zinc-400">
                reference: {activeReferenceLabel(selected)} ·{' '}
                {formatSeconds(effectiveDurationS(selected))}
                {isCroppedReference(selected) ? (
                  <span> of {formatSeconds(sourceDurationS(selected))}</span>
                ) : null}
              </p>
              <AudioBar
                label=""
                src={
                  variant && variant.kind !== 'original' && !variant.stale
                    ? artifactAudioUrl(variant.audio_artifact_id)
                    : voiceReferenceAudioUrl(selected.id, selected.updated_at)
                }
              />
              {selected.effective_transcript || selected.source_transcript ? (
                <p className="text-sm italic text-zinc-400">
                  {selected.effective_transcript || selected.source_transcript}
                </p>
              ) : null}
              <button
                type="button"
                className="w-fit rounded-md border border-zinc-500 px-3 py-1.5 text-xs text-zinc-100"
                onClick={() => openEditor()}
              >
                Open workbench
              </button>
            </>
          ) : (
            <>
              <p className="text-sm text-amber-200">Reference missing</p>
              <button
                type="button"
                className="w-fit rounded-md border border-zinc-500 px-3 py-1.5 text-xs text-zinc-100"
                onClick={() => fileRef.current?.click()}
              >
                Choose audio
              </button>
            </>
          )}
          <button
            type="button"
            className="w-fit text-xs text-red-300 underline"
            disabled={remove.isPending}
            onClick={() => {
              if (!window.confirm(`Delete voice “${selected.name}”? This cannot be undone.`)) {
                return
              }
              remove.mutate(selected.id)
            }}
          >
            {remove.isPending ? 'Deleting…' : 'Delete voice'}
          </button>
        </div>
      ) : null}

      <input
        ref={fileRef}
        type="file"
        accept={ACCEPT}
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (!file) {
            return
          }
          const stem = file.name.replace(/\.[^.]+$/, '') || 'voice'
          create.mutate({ name: stem, audio: file })
        }}
      />
      <button
        type="button"
        className="mt-auto rounded-md border border-zinc-500 px-3 py-2 text-sm text-zinc-100"
        disabled={create.isPending}
        onClick={() => fileRef.current?.click()}
      >
        {create.isPending ? 'Importing…' : '+ Import voice'}
      </button>
    </section>
  )
}
