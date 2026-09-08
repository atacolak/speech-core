import { create } from 'zustand'

export const PANE_IDS = ['voices', 'synthesis', 'inspector'] as const
export type PaneId = (typeof PANE_IDS)[number]

type WorkspaceState = {
  panes: readonly PaneId[]
}

export const useWorkspace = create<WorkspaceState>(() => ({
  panes: PANE_IDS,
}))
