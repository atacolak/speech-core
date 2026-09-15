import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef } from 'react'
import { toast } from 'sonner'
import { MaterialLibrary } from '@/features/sources/material-library'
import { fetchSources } from '@/features/sources/sources-api'
import { SourceBench } from '@/features/sources/source-bench'
import { VoiceBench } from '@/features/voices/voice-bench'
import { VoiceList } from '@/features/voices/voice-list'
import { createVoice, fetchVoices, formatApiError } from '@/lib/api'
import { useWorkspace } from '@/state/workspace'

const ACCEPT = '.wav,.mp3,.flac,.m4a,.ogg,.aac,.opus,audio/*'

/** VOICES — picking one here reads it on the bench; it does not re-point the runtime. */
function VoiceLibrary() {
  const client = useQueryClient()
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const selectVoice = useWorkspace((state) => state.selectVoice)
  const fileRef = useRef<HTMLInputElement>(null)
  const create = useMutation({
    mutationFn: createVoice,
    onSuccess: (voice) => {
      selectVoice(voice.id)
      toast.success(`Imported ${voice.name}`)
      void client.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-zinc-50">Voices</h3>
      <VoiceList
        voices={voices.data ?? []}
        selectedId={selectedVoiceId}
        onSelect={(voice) => selectVoice(voice.id)}
      />
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
        className="self-start rounded-md border border-zinc-500 px-2 py-1 text-xs text-zinc-100"
        disabled={create.isPending}
        onClick={() => fileRef.current?.click()}
      >
        {create.isPending ? 'Importing…' : '+ New voice'}
      </button>
    </div>
  )
}

/**
 * VOICE LAB — library on the left, one canvas for whatever is picked.
 * The canvas is the remaining space; nothing is allocated before it has an object.
 */
export function VoiceLab() {
  const selectedVoiceId = useWorkspace((state) => state.selectedVoiceId)
  const selectedMaterialId = useWorkspace((state) => state.selectedMaterialId)
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const sources = useQuery({ queryKey: ['sources'], queryFn: fetchSources })
  const voice = voices.data?.find((item) => item.id === selectedVoiceId)
  const material = sources.data?.find((item) => item.id === selectedMaterialId)

  return (
    <section className="flex min-h-0 flex-1 overflow-hidden">
      <aside className="flex w-72 shrink-0 flex-col gap-6 overflow-y-auto border-r border-zinc-700 p-3">
        <VoiceLibrary />
        <MaterialLibrary />
      </aside>
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4">
        {material ? (
          <SourceBench key={material.id} source={material} />
        ) : voice ? (
          <VoiceBench key={voice.id} voice={voice} />
        ) : (
          <p className="text-sm text-zinc-500">Pick a voice or some material.</p>
        )}
      </div>
    </section>
  )
}
