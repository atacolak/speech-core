import { useQuery } from '@tanstack/react-query'
import { AudioLines } from 'lucide-react'
import { RuntimeIndicator } from '@/features/runtime/runtime-indicator'
import { fetchRuntime } from '@/lib/api'
import { cn } from '@/lib/utils'

export function TopBar() {
  const runtime = useQuery({
    queryKey: ['runtime'],
    queryFn: fetchRuntime,
  })

  return (
    <header
      className={cn(
        'flex items-center gap-3 border-b border-zinc-800 bg-zinc-950 px-4 py-2',
      )}
    >
      <AudioLines className="h-4 w-4" aria-hidden />
      <h1 className="text-sm font-semibold tracking-tight">TTS lab</h1>
      <div className="ml-auto flex items-center gap-2">
        {runtime.data?.leftover_parked ? (
          <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-xs">
            leftover parked
          </span>
        ) : null}
        {runtime.data?.not_a_pin_swap ? (
          <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-xs">
            not a pin swap
          </span>
        ) : null}
        <RuntimeIndicator />
      </div>
    </header>
  )
}
