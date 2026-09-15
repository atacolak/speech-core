import { AudioLines } from 'lucide-react'
import { RuntimeIndicator } from '@/features/runtime/runtime-indicator'
import { cn } from '@/lib/utils'
import { type ModeId, useWorkspace } from '@/state/workspace'

const MODES: readonly { id: ModeId; label: string }[] = [
  { id: 'generate', label: 'GENERATE' },
  { id: 'voice-lab', label: 'VOICE LAB' },
]

export function TopBar() {
  const mode = useWorkspace((state) => state.mode)
  const selectMode = useWorkspace((state) => state.selectMode)
  const settingsOpen = useWorkspace((state) => state.settingsOpen)
  const toggleSettings = useWorkspace((state) => state.toggleSettings)

  return (
    <header
      className={cn('flex items-center gap-3 border-b border-zinc-700 bg-zinc-800 px-4 py-3')}
    >
      <AudioLines className="h-5 w-5 text-zinc-100" aria-hidden />
      <h1 className="text-base font-semibold tracking-tight text-zinc-50">TTS lab</h1>
      <nav aria-label="Lab modes" className="flex items-center gap-1">
        {MODES.map((item) => (
          <button
            key={item.id}
            type="button"
            aria-current={mode === item.id ? 'page' : undefined}
            className={cn(
              'rounded-md px-2.5 py-1 text-sm',
              mode === item.id
                ? 'bg-zinc-700 font-medium text-zinc-50'
                : 'text-zinc-300 hover:bg-zinc-700/60',
            )}
            onClick={() => selectMode(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-2">
        {mode === 'generate' ? (
          <button
            type="button"
            aria-pressed={settingsOpen}
            className={cn(
              'rounded-md px-2.5 py-1 text-sm',
              settingsOpen ? 'bg-zinc-700 text-zinc-50' : 'text-zinc-300 hover:bg-zinc-700/60',
            )}
            onClick={toggleSettings}
          >
            Settings
          </button>
        ) : null}
        <RuntimeIndicator />
      </div>
    </header>
  )
}
