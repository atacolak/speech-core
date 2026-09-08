import { Group, Panel, Separator } from 'react-resizable-panels'
import { TopBar } from '@/components/top-bar'
import { cn } from '@/lib/utils'
import { type PaneId, useWorkspace } from '@/state/workspace'

const PANE_LABEL: Record<PaneId, string> = {
  voices: 'Voices',
  synthesis: 'Synthesis',
  inspector: 'Inspector',
}

const PANE_SIZE: Record<PaneId, string> = {
  voices: '22',
  synthesis: '53',
  inspector: '25',
}

export function AppShell() {
  const panes = useWorkspace((state) => state.panes)

  return (
    <div className={cn('flex h-full min-h-screen flex-col bg-zinc-950 text-zinc-100')}>
      <TopBar />
      <Group className="min-h-0 flex-1" orientation="horizontal">
        {panes.flatMap((id, index) => {
          const panel = (
            <Panel defaultSize={PANE_SIZE[id]} id={id} key={id} minSize="12">
              <section className="h-full p-3">
                <h2 className="text-sm font-medium text-zinc-300">{PANE_LABEL[id]}</h2>
              </section>
            </Panel>
          )
          if (index === panes.length - 1) {
            return [panel]
          }
          return [
            panel,
            <Separator className="w-1 bg-zinc-800" key={`${id}-separator`} />,
          ]
        })}
      </Group>
    </div>
  )
}
