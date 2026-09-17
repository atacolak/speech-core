import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import {
  fetchVoices,
  formatApiError,
  patchVoice,
} from '@/lib/api'
import { fromStoredGeneration, toGenerationBody } from '@/lib/generation'
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
  const generation = useWorkspace((state) => state.generation)
  const patchGeneration = useWorkspace((state) => state.patchGeneration)
  const replaceGeneration = useWorkspace((state) => state.replaceGeneration)
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const voice = voices.data?.find((item) => item.id === selectedVoiceId)
  const hydratedId = useRef<string | null>(null)
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

  return (
    <section className="flex h-full min-h-0 flex-col gap-4 overflow-auto bg-zinc-800 p-4">
      <h2 className="text-base font-semibold text-zinc-50">Settings</h2>
      <div className="space-y-3 text-sm">
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
    </section>
  )
}
