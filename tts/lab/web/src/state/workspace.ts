import { create } from 'zustand'
import type { Voice } from '@/lib/api'
import { DEFAULT_GENERATION, fromStoredGeneration, type GenerationState } from '@/lib/generation'

/** Two modes; the top nav is the only way between them. */
export type ModeId = 'generate' | 'voice-lab'
export type { GenerationState }

type WorkspaceState = {
  mode: ModeId
  selectedVoiceId: string | null
  /** Set while the lab is showing material; picking a voice clears it and vice versa. */
  selectedMaterialId: string | null
  selectedRunId: string | null
  /** The voice whose stored generation currently fills `generation`; null until hydrated. */
  hydratedVoiceId: string | null
  settingsOpen: boolean
  text: string
  steer: string
  generation: GenerationState
  selectMode: (mode: ModeId) => void
  selectVoice: (id: string | null) => void
  selectMaterial: (id: string | null) => void
  selectRun: (id: string | null) => void
  hydrateGeneration: (voice: Pick<Voice, 'id' | 'generation'> | null) => void
  toggleSettings: () => void
  setText: (text: string) => void
  setSteer: (steer: string) => void
  patchGeneration: (patch: Partial<GenerationState>) => void
  replaceGeneration: (generation: GenerationState) => void
}

export const useWorkspace = create<WorkspaceState>((set) => ({
  mode: 'generate',
  selectedVoiceId: null,
  selectedMaterialId: null,
  selectedRunId: null,
  hydratedVoiceId: null,
  settingsOpen: false,
  text: "you don't need kubernetes. you need one process that doesn't suck. if it dies, restart it. congratulations, you invented infrastructure.",
  steer: 'fast, dry, technically confident, faintly amused.',
  generation: { ...DEFAULT_GENERATION },
  selectMode: (mode) => set({ mode }),
  selectVoice: (id) =>
    set({ selectedVoiceId: id, selectedMaterialId: null, selectedRunId: null, hydratedVoiceId: null }),
  selectMaterial: (id) => set({ selectedMaterialId: id }),
  selectRun: (id) => set({ selectedRunId: id }),
  hydrateGeneration: (voice) =>
    set((state) => {
      if (!voice) {
        return { hydratedVoiceId: null }
      }
      if (state.hydratedVoiceId === voice.id) {
        return state
      }
      return {
        hydratedVoiceId: voice.id,
        generation: fromStoredGeneration(voice.generation),
      }
    }),
  toggleSettings: () => set((state) => ({ settingsOpen: !state.settingsOpen })),
  setText: (text) => set({ text }),
  setSteer: (steer) => set({ steer }),
  patchGeneration: (patch) =>
    set((state) => ({ generation: { ...state.generation, ...patch } })),
  replaceGeneration: (generation) => set({ generation: { ...generation } }),
}))
