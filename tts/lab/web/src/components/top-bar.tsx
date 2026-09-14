import { AudioLines } from 'lucide-react'
import { RuntimeIndicator } from '@/features/runtime/runtime-indicator'
import { cn } from '@/lib/utils'
import { type BenchId, useWorkspace } from '@/state/workspace'

const BENCHES: readonly { id: BenchId; label: string }[] = [
  { id: 'voices', label: 'Voices' },
  { id: 'sources', label: 'Sources' },
]

export function TopBar() {
  const bench = useWorkspace((state) => state.bench)
  const selectBench = useWorkspace((state) => state.selectBench)

  return (
    <header
      className={cn('flex items-center gap-3 border-b border-zinc-700 bg-zinc-800 px-4 py-3')}
    >
      <AudioLines className="h-5 w-5 text-zinc-100" aria-hidden />
      <h1 className="text-base font-semibold tracking-tight text-zinc-50">TTS lab</h1>
      <nav aria-label="Benches" className="flex items-center gap-1">
        {BENCHES.map((item) => (
          <button
            key={item.id}
            type="button"
            aria-current={bench === item.id ? 'page' : undefined}
            className={cn(
              'rounded-md px-2.5 py-1 text-sm',
              bench === item.id
                ? 'bg-zinc-700 font-medium text-zinc-50'
                : 'text-zinc-300 hover:bg-zinc-700/60',
            )}
            onClick={() => selectBench(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-2">
        <RuntimeIndicator />
      </div>
    </header>
  )
}
