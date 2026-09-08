import { useQuery } from '@tanstack/react-query'
import { fetchRuntime } from '@/lib/api'
import { cn } from '@/lib/utils'

export function RuntimeIndicator() {
  const query = useQuery({
    queryKey: ['runtime'],
    queryFn: fetchRuntime,
  })

  const status = query.isError ? 'error' : query.isPending ? 'loading' : (query.data?.status ?? 'loading')

  return (
    <div className={cn('flex items-center gap-2 text-xs text-zinc-300')}>
      <span
        aria-label={`runtime ${status}`}
        className={cn(
          'inline-block h-2 w-2 rounded-full',
          status === 'ready' && 'bg-emerald-400',
          status === 'loading' && 'bg-amber-400',
          status === 'error' && 'bg-red-500',
        )}
        data-status={status}
        data-testid="runtime-status"
      />
      {query.data?.selected ? <span>{query.data.selected}</span> : null}
    </div>
  )
}
