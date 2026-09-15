import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { AudioBar } from '@/components/audio-bar'
import { VoiceList } from '@/features/voices/voice-list'
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
  patchVoice,
  pickDefaultVoice,
  setActiveVoice,
  sourceDurationS,
  voiceReferenceAudioUrl,
} from '@/lib/api'
import { formatSeconds } from '@/lib/format'
import { useWorkspace } from '@/state/workspace'

const ACCEPT = '.wav,.mp3,.flac,.m4a,.ogg,.aac,.opus,audio/*'

export function VoicesPane() {
  const client = useQueryClient()
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const selectVoice = useWorkspace((state) => state.selectVoice)
  const openEditor = useWorkspace((state) => state.openEditor)
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: fetchRuntime })
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
      {voices.isError ? (
        <p className="text-sm text-red-400">Could not load voices.</p>
      ) : (
        <VoiceList
          voices={voices.data ?? []}
          selectedId={selectedVoiceId}
          onSelect={(voice) => {
            selectVoice(voice.id)
            void setActiveVoice(voice.id)
          }}
        />
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
