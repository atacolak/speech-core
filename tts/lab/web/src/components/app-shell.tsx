import { Group, Panel, Separator } from 'react-resizable-panels'
import { TopBar } from '@/components/top-bar'
import { InspectorPane } from '@/features/inspector/inspector-pane'
import { SourcesView } from '@/features/sources/sources-view'
import { SynthesisPane } from '@/features/synthesis/synthesis-pane'
import { VoicesPane } from '@/features/voices/voices-pane'
import { VoiceWorkbench } from '@/features/voices/workbench'
import { cn } from '@/lib/utils'
import { type PaneId, useWorkspace } from '@/state/workspace'

const PANE_SIZE: Record<PaneId, string> = {
  voices: '24',
  synthesis: '48',
  inspector: '28',
}

function PaneBody({ id }: { id: PaneId }) {
  if (id === 'voices') {
    return <VoicesPane />
  }
  if (id === 'synthesis') {
    return <SynthesisPane />
  }
  return <InspectorPane />
}

export function AppShell() {
  const panes = useWorkspace((state) => state.panes)
  const bench = useWorkspace((state) => state.bench)
  const editorOpen = useWorkspace((state) => state.editorOpen)

  return (
    <div className={cn('relative flex h-full min-h-screen flex-col bg-zinc-800 text-zinc-100')}>
      <TopBar />
      {bench === 'sources' ? (
        <div className="flex min-h-0 flex-1">
          <SourcesView />
        </div>
      ) : editorOpen ? (
        <VoiceWorkbench />
      ) : (
        <Group className="min-h-0 flex-1" orientation="horizontal">
          {panes.flatMap((id, index) => {
            const panel = (
              <Panel
                className="bg-zinc-800"
                defaultSize={PANE_SIZE[id]}
                id={id}
                key={id}
                minSize="14"
              >
                <PaneBody id={id} />
              </Panel>
            )
            if (index === panes.length - 1) {
              return [panel]
            }
            return [panel, <Separator className="w-1 bg-zinc-500" key={`${id}-separator`} />]
          })}
        </Group>
      )}
    </div>
  )
}
