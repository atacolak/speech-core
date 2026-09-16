import { Group, Panel, Separator } from 'react-resizable-panels'
import { TopBar } from '@/components/top-bar'
import { InspectorPane } from '@/features/inspector/inspector-pane'
import { SynthesisPane } from '@/features/synthesis/synthesis-pane'
import { VoiceLab } from '@/features/voice-lab/voice-lab'
import { VoicesPane } from '@/features/voices/voices-pane'
import { VoiceWorkbench } from '@/features/voices/workbench'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/state/workspace'

export function AppShell() {
  const mode = useWorkspace((state) => state.mode)
  const editorOpen = useWorkspace((state) => state.editorOpen)
  const settingsOpen = useWorkspace((state) => state.settingsOpen)

  return (
    <div className={cn('relative flex h-full min-h-screen flex-col bg-zinc-800 text-zinc-100')}>
      <TopBar />
      {mode === 'generate' ? (
        <Group className="min-h-0 flex-1" orientation="horizontal">
          <Panel className="bg-zinc-800" defaultSize="24" id="voices" minSize="14">
            <VoicesPane />
          </Panel>
          <Separator className="w-1 bg-zinc-500" />
          <Panel className="bg-zinc-900" defaultSize="48" id="synthesis" minSize="24">
            <SynthesisPane />
          </Panel>
          {settingsOpen ? (
            <>
              <Separator className="w-1 bg-zinc-500" />
              <Panel className="bg-zinc-800" defaultSize="28" id="settings" minSize="14">
                <InspectorPane />
              </Panel>
            </>
          ) : null}
        </Group>
      ) : editorOpen ? (
        <VoiceWorkbench />
      ) : (
        <VoiceLab />
      )}
    </div>
  )
}
