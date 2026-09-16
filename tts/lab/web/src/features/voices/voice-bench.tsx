import { useQuery } from '@tanstack/react-query'
import { AudioBar } from '@/components/audio-bar'
import { type Voice, artifactAudioUrl, fetchRuns } from '@/lib/api'
import { formatClock, formatMs, formatSeconds } from '@/lib/format'
import {
  type GenerationRow,
  type OriginalRow,
  voiceAssets,
  voiceGenerations,
} from '@/features/voices/voice-assets'

/** Each level of lineage steps in, so a child reads as belonging to the row above it. */
const INDENT_PX = 12

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
    <section
      aria-label={title}
      className="rounded-md border border-zinc-700 bg-zinc-900 p-3"
      role="region"
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">{title}</h3>
      <p className="mt-1 text-[11px] text-zinc-500">{hint}</p>
      {children}
    </section>
  )
}

/** One ORIGINALS row: its own audio, what it was derived from, and what came out of it. */
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
  return (
    <li
      className="flex flex-col gap-1"
      data-depth={depth}
      style={{ marginLeft: depth * INDENT_PX }}
    >
      <span className="text-xs text-zinc-200">
        {row.reference ? <span className="mr-1 text-amber-300">★</span> : null}
        <span>{row.label}</span>
        {row.durationS == null ? null : ` · ${formatSeconds(row.durationS)}`}
        {row.stale ? <span className="text-amber-200"> · stale</span> : null}
        {row.derivedFrom ? <span className="text-zinc-500"> derived from {row.derivedFrom}</span> : null}
      </span>
      {row.transcript ? <p className="text-[11px] italic text-zinc-500">{row.transcript}</p> : null}
      <AudioBar src={artifactAudioUrl(row.audioArtifactId)} label="" />
      {children.length === 0 ? null : (
        <ul className="flex flex-col gap-2">
          {children.map((child) => (
            <OriginalRowView
              childrenByParent={childrenByParent}
              depth={depth + 1}
              key={child.id}
              row={child}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

/** One GENERATIONS row: Breeze's own audio and what it cost. A take never wears the ★. */
function GenerationRowView({ row }: { row: GenerationRow }) {
  return (
    <li className="flex flex-col gap-1">
      <span className="text-xs text-zinc-200">
        <span>{row.label}</span>
        {` · ${formatSeconds(row.durationS)}`}
        {` · ${formatMs(row.firstAudioMs)} first audio`}
        {row.createdAt ? (
          <span className="text-zinc-500"> · {formatClock(row.createdAt)}</span>
        ) : null}
      </span>
      <AudioBar src={artifactAudioUrl(row.audioArtifactId)} label="" />
    </li>
  )
}

/**
 * VOICE BENCH — the selected voice's store. ORIGINALS is what the voice is made
 * of, lineage included; GENERATIONS is what Breeze produced from it. Read, to pick one.
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
    <div className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold text-zinc-50">Voice store · {voice.name}</h2>
      <VoiceSection
        title="ORIGINALS"
        hint="The voice's own material and everything derived from it. ★ is the reference Breeze may use."
      >
        {assets.roots.length === 0 ? (
          <p className="mt-2 text-xs text-zinc-500">No originals yet.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-2">
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
      <VoiceSection
        title="GENERATIONS"
        hint="Breeze takes recorded against this voice. A generation is never a reference."
      >
        {generations.length === 0 ? (
          <p className="mt-2 text-xs text-zinc-500">No takes yet.</p>
        ) : (
          <ul className="mt-2 flex flex-col gap-2">
            {generations.map((row) => (
              <GenerationRowView key={row.id} row={row} />
            ))}
          </ul>
        )}
      </VoiceSection>
    </div>
  )
}
