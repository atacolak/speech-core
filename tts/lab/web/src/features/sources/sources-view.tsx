import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import { SourceBench } from '@/features/sources/source-bench'
import {
  type MediaSource,
  addSourceFile,
  addSourceUrl,
  fetchSources,
  isUnavailable,
} from '@/features/sources/sources-api'
import { cn } from '@/lib/utils'

const ACCEPT = '.wav,.mp3,.flac,.m4a,.ogg,.aac,.opus,audio/*,video/*'

function SourceRow({
  source,
  selected,
  onSelect,
}: {
  source: MediaSource
  selected: boolean
  onSelect: () => void
}) {
  const state =
    source.coverage.length > 0
      ? `${source.speakers.length} speaker(s) · ${source.clips.length} clip(s)`
      : 'not analyzed'
  return (
    <li>
      <button
        type="button"
        className={cn(
          'w-full rounded-md px-2.5 py-1.5 text-left',
          selected ? 'bg-zinc-700 text-zinc-50' : 'text-zinc-200 hover:bg-zinc-800',
        )}
        onClick={onSelect}
      >
        <span className="block truncate text-sm font-medium">{source.title}</span>
        <span className="block text-[11px] text-zinc-400">
          {source.kind} · {source.duration_s.toFixed(1)}s · {state}
        </span>
        <span className="mt-0.5 block truncate text-[11px] text-zinc-500">{source.origin}</span>
      </button>
    </li>
  )
}

export function SourcesView() {
  const client = useQueryClient()
  const sources = useQuery({ queryKey: ['sources'], queryFn: fetchSources })
  const [query, setQuery] = useState('')
  const [url, setUrl] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const add = useMutation({
    mutationFn: (input: { file: File } | { url: string }) =>
      'file' in input ? addSourceFile(input.file) : addSourceUrl(input.url),
    onSuccess: (source) => {
      toast.success(`Added ${source.title}`)
      setUrl('')
      setSelectedId(source.id)
      void client.invalidateQueries({ queryKey: ['sources'] })
    },
    onError: (error) =>
      toast.error(
        isUnavailable(error) ? 'Source ingest is not available in this lab build yet.' : 'Ingest failed.',
      ),
  })

  const items = sources.data ?? []
  const needle = query.trim().toLowerCase()
  const visible = needle
    ? items.filter(
        (source) =>
          source.title.toLowerCase().includes(needle) ||
          source.origin.toLowerCase().includes(needle),
      )
    : items
  const selected = items.find((source) => source.id === selectedId)

  return (
    <section className="flex min-h-0 flex-1 overflow-hidden bg-zinc-800">
      <aside className="flex w-64 shrink-0 flex-col gap-2 overflow-y-auto border-r border-zinc-700 p-3">
        <h2 className="text-sm font-semibold text-zinc-50">Sources</h2>
        <input
          className="w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm text-zinc-100"
          placeholder="Search sources"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <label className="text-[11px] uppercase tracking-wide text-zinc-500" htmlFor="source-url">
          Source URL
        </label>
        <div className="flex gap-1">
          <input
            className="min-w-0 flex-1 rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm text-zinc-100"
            id="source-url"
            placeholder="https://www.youtube.com/watch?v=…"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
          />
          <button
            type="button"
            className="shrink-0 rounded-md border border-zinc-500 px-2 py-1 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
            disabled={add.isPending || url.trim().length === 0}
            onClick={() => add.mutate({ url: url.trim() })}
          >
            + Add source
          </button>
        </div>
        <label className="text-[11px] uppercase tracking-wide text-zinc-500" htmlFor="source-file">
          Source file
        </label>
        <input
          ref={fileRef}
          className="w-full text-xs text-zinc-300"
          id="source-file"
          type="file"
          accept={ACCEPT}
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file) {
              add.mutate({ file })
            }
          }}
        />
        {sources.isError ? (
          <p className="text-sm text-red-400">Could not load sources.</p>
        ) : items.length === 0 ? (
          <p className="text-sm text-zinc-400">No sources yet. Add a URL or a local file to start a bench.</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {visible.map((source) => (
              <SourceRow
                key={source.id}
                source={source}
                selected={source.id === selectedId}
                onSelect={() => setSelectedId(source.id)}
              />
            ))}
          </ul>
        )}
      </aside>
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4">
        {selected ? (
          <SourceBench key={selected.id} source={selected} />
        ) : (
          <p className="text-sm text-zinc-500">Pick a source.</p>
        )}
      </div>
    </section>
  )
}
