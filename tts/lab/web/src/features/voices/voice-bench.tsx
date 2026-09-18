import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState, type MouseEvent } from 'react'
import { ChevronDown, ChevronRight, Pause, Play } from 'lucide-react'
import { toast } from 'sonner'
import { type Voice, artifactAudioUrl, fetchRuns, formatApiError, renameRun } from '@/lib/api'
import { formatSeconds } from '@/lib/format'
import {
  type GenerationRow,
  type OriginalRow,
  voiceAssets,
  voiceGenerations,
} from '@/features/voices/voice-assets'

/** Each level of lineage steps in, so a child reads as belonging to the row above it. */
const INDENT_PX = 12

/** Icon transport over a hidden HTMLAudioElement — never native controls. */
function StoreTransport({ src }: { src: string }) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const toggle = (event: MouseEvent) => {
    event.stopPropagation()
    const audio = audioRef.current
    if (!audio) {
      return
    }
    if (playing) {
      audio.pause()
      setPlaying(false)
      return
    }
    void audio.play().then(
      () => setPlaying(true),
      () => setPlaying(false),
    )
  }
  return (
    <span className="inline-flex shrink-0 items-center">
      <button
        aria-label={playing ? 'Pause' : 'Play'}
        className="rounded p-0.5 text-zinc-200"
        onClick={toggle}
        type="button"
      >
        {playing ? (
          <Pause aria-hidden="true" className="h-3.5 w-3.5" />
        ) : (
          <Play aria-hidden="true" className="h-3.5 w-3.5" />
        )}
      </button>
      <audio
        className="hidden"
        onEnded={() => setPlaying(false)}
        preload="metadata"
        ref={audioRef}
        src={src}
      />
    </span>
  )
}

function VoiceSection({
  title,
  hint,
  children,
}: {
  title: string
  hint: string
  children?: React.ReactNode
}) {
  return (
    <section aria-label={title} className="flex flex-col gap-1" role="region">
      <h3 className="text-xs font-medium text-zinc-400">{title}</h3>
      <p className="text-[11px] text-zinc-500">{hint}</p>
      {children}
    </section>
  )
}

/** One Originals row: compact play + label + duration; transcript and derived stay folded. */
function OriginalRowView({
  row,
  childrenByParent,
  depth,
}: {
  row: OriginalRow
  childrenByParent: Map<string, OriginalRow[]>
  depth: number
}) {
  const children = childrenByParent.get(row.id) ?? []
  const [expanded, setExpanded] = useState(false)
  const [derivedOpen, setDerivedOpen] = useState(false)
  return (
    <li
      className="flex flex-col gap-0.5"
      data-depth={depth}
      style={{ marginLeft: depth * INDENT_PX }}
    >
      <div
        className="flex cursor-pointer items-center gap-2 text-xs text-zinc-200"
        onClick={() => setExpanded((open) => !open)}
      >
        <StoreTransport src={artifactAudioUrl(row.audioArtifactId)} />
        <button
          aria-label={expanded ? 'Hide transcript' : 'Show transcript'}
          className="rounded p-0.5 text-zinc-500"
          onClick={(event) => {
            event.stopPropagation()
            setExpanded((open) => !open)
          }}
          type="button"
        >
          {expanded ? (
            <ChevronDown aria-hidden="true" className="h-3 w-3" />
          ) : (
            <ChevronRight aria-hidden="true" className="h-3 w-3" />
          )}
        </button>
        {row.reference ? <span className="text-amber-300">★</span> : null}
        <span>{row.label}</span>
        {row.durationS == null ? null : (
          <span className="shrink-0 text-zinc-400">{formatSeconds(row.durationS)}</span>
        )}
        {row.stale ? <span className="text-amber-200">· stale</span> : null}
        {children.length === 0 ? null : (
          <button
            className="shrink-0 text-[11px] text-zinc-500"
            onClick={(event) => {
              event.stopPropagation()
              setDerivedOpen((open) => !open)
            }}
            type="button"
          >
            {children.length} derived
          </button>
        )}
      </div>
      {expanded && row.transcript ? (
        <p className="pl-8 text-[11px] italic text-zinc-500">{row.transcript}</p>
      ) : null}
      {derivedOpen && children.length > 0 ? (
        <ul className="flex flex-col gap-0.5">
          {children.map((child) => (
            <OriginalRowView
              childrenByParent={childrenByParent}
              depth={depth + 1}
              key={child.id}
              row={child}
            />
          ))}
        </ul>
      ) : null}
    </li>
  )
}

/** One Takes row: compact play + rename + duration. A take never wears the ★. */
function GenerationRowView({ row }: { row: GenerationRow }) {
  const named = row.label !== 'Untitled'
  const [title, setTitle] = useState(named ? row.label : '')
  const client = useQueryClient()
  const rename = useMutation({
    mutationFn: (name: string) => renameRun(row.id, name),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['runs'] })
      void client.invalidateQueries({ queryKey: ['voices'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })
  const commitTitle = () => {
    const next = title.trim()
    if (!next || next === (named ? row.label : '')) {
      setTitle(named ? row.label : '')
      return
    }
    rename.mutate(next)
  }
  return (
    <li className="flex flex-col gap-0.5">
      <span className="flex items-center gap-2 text-xs text-zinc-200">
        <StoreTransport src={artifactAudioUrl(row.audioArtifactId)} />
        <input
          aria-label="Take title"
          className="min-w-0 flex-1 bg-transparent text-xs text-zinc-200 outline-none placeholder:text-zinc-500"
          onBlur={commitTitle}
          onChange={(event) => setTitle(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              commitTitle()
            }
          }}
          placeholder="Untitled"
          value={title}
        />
        {row.durationS == null ? null : (
          <span className="shrink-0 text-zinc-400">{formatSeconds(row.durationS)}</span>
        )}
      </span>
    </li>
  )
}

/**
 * VOICE BENCH — the selected voice's store. Originals is what the voice is made
 * of, lineage included; Takes is what Breeze produced from it.
 */
export function VoiceBench({ voice }: { voice: Voice | undefined }) {
  const runs = useQuery({
    queryKey: ['runs', voice?.id],
    queryFn: () => fetchRuns(voice?.id),
    enabled: Boolean(voice),
  })

  if (!voice) {
    return null
  }

  const assets = voiceAssets(voice)
  const generations = voiceGenerations(runs.data ?? [])

  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold text-zinc-50">{voice.name}</h2>
      <VoiceSection title="Originals" hint="Human material. ★ is the enrolled reference.">
        {assets.roots.length === 0 ? (
          <p className="text-xs text-zinc-500">No originals yet.</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {assets.roots.map((row) => (
              <OriginalRowView
                childrenByParent={assets.childrenByParent}
                depth={0}
                key={row.id}
                row={row}
              />
            ))}
          </ul>
        )}
      </VoiceSection>
      <VoiceSection title="Takes" hint="Name a take to use it as a GENERATE reference.">
        {generations.length === 0 ? (
          <p className="text-xs text-zinc-500">No takes yet.</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {generations.map((row) => (
              <GenerationRowView key={row.id} row={row} />
            ))}
          </ul>
        )}
      </VoiceSection>
    </div>
  )
}
