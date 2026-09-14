import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { fetchRuntime, formatBytes, loadE2, unloadE2, type RuntimeInfo } from '@/lib/api'
import { cn } from '@/lib/utils'

const DOT: Record<string, string> = {
  unloaded: 'border border-zinc-400 bg-transparent',
  loading: 'bg-amber-400',
  unloading: 'bg-amber-400',
  ready: 'bg-emerald-400',
  insufficient_vram: 'bg-red-500',
  error: 'bg-red-500',
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

async function waitWhile(states: string[]): Promise<RuntimeInfo> {
  for (let i = 0; i < 480; i += 1) {
    const status = await fetchRuntime()
    if (!states.includes(status.state)) {
      return status
    }
    await delay(700)
  }
  return fetchRuntime()
}

function modelName(info: RuntimeInfo | undefined): string {
  return info?.display_name?.trim() || 'Breeze TTS2'
}

function vramError(info: RuntimeInfo | undefined): string {
  const err = info?.last_error
  const name = modelName(info)
  if (err?.code === 'insufficient_vram') {
    return `couldn't load ${name} · ${formatBytes(err.free_bytes)} free · ${formatBytes(err.required_bytes)} required`
  }
  if (err?.message) {
    return err.message
  }
  return `couldn't load ${name}`
}

export function RuntimeIndicator() {
  const client = useQueryClient()
  const query = useQuery({
    queryKey: ['runtime'],
    queryFn: fetchRuntime,
    refetchInterval: (current) => {
      const state = current.state.data?.state
      return state === 'loading' || state === 'unloading' ? 700 : 2500
    },
  })
  const load = useMutation({
    mutationFn: async () => {
      const started = await loadE2()
      if (started.state === 'loading') {
        return waitWhile(['loading'])
      }
      return started
    },
    onSuccess: (body) => {
      void client.invalidateQueries({ queryKey: ['runtime'] })
      if (body.state === 'insufficient_vram') {
        toast.error(vramError(body))
        return
      }
      if (body.state === 'error') {
        toast.error(vramError(body))
      }
    },
    onError: (error) => toast.error(String(error)),
  })
  const unload = useMutation({
    mutationFn: async () => {
      const started = await unloadE2()
      if (started.state === 'unloading') {
        return waitWhile(['unloading'])
      }
      return started
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtime'] }),
    onError: (error) => toast.error(String(error)),
  })

  const state = query.isError ? 'error' : (query.data?.state ?? query.data?.status ?? 'unloaded')
  const name = modelName(query.data)
  const loading = state === 'loading' || load.isPending
  const [now, setNow] = useState(() => Date.now())
  const [clientStarted, setClientStarted] = useState<number | null>(null)
  useEffect(() => {
    if (!loading && state !== 'unloading') {
      setClientStarted(null)
      return
    }
    setClientStarted((prev) => prev ?? Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(id)
  }, [loading, state])
  const startedMs = query.data?.load_started_at ? Date.parse(query.data.load_started_at) : Number.NaN
  const elapsedS = loading
    ? Math.max(
        0,
        Math.floor(
          (now -
            (Number.isFinite(startedMs)
              ? startedMs
              : (clientStarted ?? now))) /
            1000,
        ),
      )
    : null
  const busy = load.isPending || unload.isPending || state === 'loading' || state === 'unloading'
  const clickable =
    !busy &&
    (state === 'unloaded' ||
      state === 'insufficient_vram' ||
      state === 'error' ||
      state === 'ready')

  const label =
    state === 'insufficient_vram'
      ? `${name} unloaded`
      : state === 'error'
        ? `${name} error`
        : `${name} ${state}`

  const title =
    state === 'ready'
      ? `Unload ${name} and return GPU VRAM`
      : state === 'loading' || state === 'unloading'
        ? label
        : `Load ${name} if the GPU has enough free VRAM`

  const onClick = () => {
    if (!clickable) {
      return
    }
    if (state === 'ready') {
      unload.mutate()
      return
    }
    load.mutate()
  }

  return (
    <div className="flex flex-wrap items-center justify-end gap-2 text-xs text-zinc-200">
      <button
        type="button"
        aria-label={label}
        title={title}
        disabled={!clickable}
        data-status={state}
        data-testid="runtime-status"
        className={cn(
          'inline-flex items-center gap-2 rounded-full border px-2.5 py-1',
          clickable
            ? 'cursor-pointer border-zinc-400 text-zinc-50 hover:border-zinc-200 hover:bg-zinc-700'
            : 'cursor-default border-zinc-600 text-zinc-300',
          state === 'insufficient_vram' || state === 'error' ? 'border-red-400 text-red-200' : null,
        )}
        onClick={onClick}
      >
        <span
          className={cn('inline-block h-2.5 w-2.5 rounded-full', DOT[state] ?? 'bg-zinc-500')}
        />
        <span>{label}</span>
      </button>
      {loading ? (
        <span className="max-w-xs text-[11px] text-zinc-400">
          {[
            query.data?.load_phase,
            elapsedS != null ? `${elapsedS}s` : null,
            query.data?.used_vram_bytes != null
              ? `${formatBytes(query.data.used_vram_bytes)} VRAM`
              : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        </span>
      ) : null}
      {state === 'insufficient_vram' && query.data?.last_error ? (
        <span className="max-w-xs text-[11px] text-red-300">
          insufficient VRAM · {formatBytes(query.data.last_error.free_bytes)} free ·{' '}
          {formatBytes(query.data.last_error.required_bytes)} required
        </span>
      ) : null}
    </div>
  )
}
