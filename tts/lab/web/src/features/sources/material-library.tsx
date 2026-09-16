import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  type MediaSource,
  addSourceArtifact,
  addSourceFile,
  addSourceUrl,
  fetchSources,
  isUnavailable,
} from '@/features/sources/sources-api'
import { type RunItem, fetchRuns } from '@/lib/api'
import { useWorkspace } from '@/state/workspace'
import { cn } from '@/lib/utils'

const ACCEPT = '.wav,.mp3,.flac,.m4a,.ogg,.aac,.opus,audio/*,video/*'

const INGEST_UNAVAILABLE = 'Source ingest is not available in this lab build yet.'

/** What a source counts as in the lab: material a voice can be cut out of. */
function MaterialRow({
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
        aria-current={selected ? 'true' : undefined}
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

/** MATERIAL CORPUS — the sources the lab cuts voices out of. */
export function MaterialLibrary() {
  const client = useQueryClient()
  const selectedMaterialId = useWorkspace((state) => state.selectedMaterialId)
  const selectMaterial = useWorkspace((state) => state.selectMaterial)
  const sources = useQuery({ queryKey: ['sources'], queryFn: fetchSources })
  const runs = useQuery({ queryKey: ['runs'], queryFn: () => fetchRuns() })
  const [query, setQuery] = useState('')
  const [url, setUrl] = useState('')
  const [takeId, setTakeId] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const retained = runs.data ?? []
  const pickedTake = retained.find((run) => run.id === takeId)
  const add = useMutation({
    mutationFn: (input: { file: File } | { url: string } | { run: RunItem }) => {
      if ('file' in input) {
        return addSourceFile(input.file)
      }
      if ('url' in input) {
        return addSourceUrl(input.url)
      }
      // One retained take, by its existing artifact: no copy, no history import.
      return addSourceArtifact({
        artifactId: input.run.output_artifact_id,
        runId: input.run.id,
        title: `take ${input.run.id}`,
      })
    },
    onSuccess: (source) => {
      toast.success(`Added ${source.title}`)
      setUrl('')
      setTakeId(null)
      selectMaterial(source.id)
      void client.invalidateQueries({ queryKey: ['sources'] })
    },
    onError: (error) => toast.error(isUnavailable(error) ? INGEST_UNAVAILABLE : 'Ingest failed.'),
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

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-zinc-50">Material</h3>
      <input
        className="w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm text-zinc-100"
        placeholder="Search material"
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
      {retained.length === 0 ? null : (
        <div className="flex flex-col gap-1">
          <h4 className="text-[11px] uppercase tracking-wide text-zinc-500">Retained takes</h4>
          <ul aria-label="retained takes" className="flex flex-col gap-0.5">
            {retained.map((run) => (
              <li key={run.id}>
                <button
                  type="button"
                  aria-pressed={run.id === takeId}
                  className={cn(
                    'w-full rounded-md px-2 py-1 text-left text-xs',
                    run.id === takeId
                      ? 'bg-zinc-700 text-zinc-50'
                      : 'text-zinc-300 hover:bg-zinc-800',
                  )}
                  onClick={() => setTakeId(run.id)}
                >
                  take {run.id}
                  {run.duration_s == null ? null : (
                    <span className="ml-2 text-[11px] text-zinc-500">
                      {run.duration_s.toFixed(1)}s
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="self-start rounded-md border border-zinc-500 px-2 py-1 text-xs text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
            disabled={!pickedTake || add.isPending}
            onClick={() => {
              if (pickedTake) {
                add.mutate({ run: pickedTake })
              }
            }}
          >
            Add to workbench
          </button>
        </div>
      )}
      {sources.isError ? (
        <p className="text-sm text-red-400">Could not load material.</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-zinc-400">No material yet. Add a URL or a local file.</p>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {visible.map((source) => (
            <MaterialRow
              key={source.id}
              source={source}
              selected={source.id === selectedMaterialId}
              onSelect={() => selectMaterial(source.id)}
            />
          ))}
        </ul>
      )}
    </div>
  )
}
