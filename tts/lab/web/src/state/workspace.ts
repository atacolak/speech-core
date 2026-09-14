import { create } from 'zustand'
import { DEFAULT_GENERATION, type GenerationState } from '@/lib/generation'

export const PANE_IDS = ['voices', 'synthesis', 'inspector'] as const
export type PaneId = (typeof PANE_IDS)[number]
/** The lab has two benches; the top nav is the only way between them. */
export type BenchId = 'voices' | 'sources'
export type { GenerationState }

type WorkspaceState = {
  panes: readonly PaneId[]
  bench: BenchId
  selectedVoiceId: string | null
  selectedRunId: string | null
  editorOpen: boolean
  text: string
  steer: string
  generation: GenerationState
  selectBench: (bench: BenchId) => void
  selectVoice: (id: string | null) => void
  selectRun: (id: string | null) => void
  openEditor: () => void
  closeEditor: () => void
  setText: (text: string) => void
  setSteer: (steer: string) => void
  patchGeneration: (patch: Partial<GenerationState>) => void
  replaceGeneration: (generation: GenerationState) => void
}

export const useWorkspace = create<WorkspaceState>((set) => ({
  panes: PANE_IDS,
  bench: 'voices',
  selectedVoiceId: null,
  selectedRunId: null,
  editorOpen: false,
  text: "you don't need kubernetes. you need one process that doesn't suck. if it dies, restart it. congratulations, you invented infrastructure.",
  steer: 'fast, dry, technically confident, faintly amused.',
  generation: { ...DEFAULT_GENERATION },
  selectBench: (bench) => set({ bench }),
  selectVoice: (id) => set({ selectedVoiceId: id, selectedRunId: null }),
  selectRun: (id) => set({ selectedRunId: id }),
  openEditor: () => set({ editorOpen: true }),
  closeEditor: () => set({ editorOpen: false }),
  setText: (text) => set({ text }),
  setSteer: (steer) => set({ steer }),
  patchGeneration: (patch) =>
    set((state) => ({ generation: { ...state.generation, ...patch } })),
  replaceGeneration: (generation) => set({ generation: { ...generation } }),
}))
