import { useQuery } from '@tanstack/react-query'
import { AudioBar } from '@/components/audio-bar'
import {
  type Voice,
  type VoiceArtifact,
  artifactAudioUrl,
  fetchRuns,
  isCroppedReference,
  sourceDurationS,
  effectiveDurationS,
} from '@/lib/api'
import { formatClock, formatMs, formatSeconds } from '@/lib/format'
import { voiceClips } from '@/features/sources/sources-api'

/** Reference kinds are internal processor names; the bench names the outcome.
 *  Kept in step with `REFERENCE_WORDS` in lib/api.ts until that map is exported. */
const KIND_WORDS: Record<string, string> = {
  original: 'original',
  resemble: 'denoised',
  auk: 'candidate',
}

function kindWord(kind: string): string {
  return KIND_WORDS[kind] ?? kind
}

/** A row in the bench. Voice variants know their length; backend artifacts do not. */
type BenchArtifact = VoiceArtifact & { duration_s?: number }

function BenchSection({
  title,
  hint,
  empty,
  children,
}: {
  title: string
  hint?: string
  empty: boolean
  children?: React.ReactNode
}) {
  if (empty) {
    return null
  }
  return (
    <section
      aria-label={title}
      className="rounded-md border border-zinc-700 bg-zinc-900 p-3"
      role="region"
    >
      <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">{title}</h3>
      {hint ? <p className="mt-1 text-[11px] text-zinc-500">{hint}</p> : null}
      {children}
    </section>
  )
}


/** Approved reference material, with the ★ default in front. */
function referencesOf(voice: Voice): BenchArtifact[] {
  const artifacts = voice.artifacts ?? []
  const approved = artifacts.filter((item) => item.role === 'reference')
  if (approved.length > 0) {
    const wanted = voice.default_reference_id ?? null
    return [...approved].sort((left, right) => {
      const star = Number(right.id === wanted) - Number(left.id === wanted)
      return star !== 0 ? star : Number(Boolean(right.default)) - Number(Boolean(left.default))
    })
  }
  const variant = voice.active_variant
  const playable = variant && !variant.stale ? variant : null
  if (!playable && !voice.source_audio_artifact_id) {
    return []
  }
  return [
    {
      id: playable?.id ?? voice.active_reference_variant_id ?? voice.source_audio_artifact_id,
      role: 'reference',
      kind: variant?.kind ?? 'original',
      // A stale variant was rendered from material that has since changed: play the crop, as the pane does.
      audio_artifact_id: playable?.audio_artifact_id ?? voice.source_audio_artifact_id,
      duration_s: playable?.duration_s ?? effectiveDurationS(voice),
      stale: Boolean(variant?.stale),
      default: true,
    },
  ]
}

/** Resemble denoise/enhance and any other post-extraction processor output. */
function derivativesOf(voice: Voice): BenchArtifact[] {
  const artifacts = voice.artifacts ?? []
  const fromArtifacts = artifacts.filter(
    (artifact) => artifact.role !== 'reference' && artifact.kind !== 'original',
  )
  if (fromArtifacts.length > 0) {
    return fromArtifacts
  }
  return (voice.variants ?? [])
    .filter((variant) => variant.kind !== 'original')
    .map((variant) => ({
      id: variant.id,
      role: 'experiment' as const,
      kind: variant.kind,
      audio_artifact_id: variant.audio_artifact_id,
      duration_s: variant.duration_s,
      stale: variant.stale,
    }))
}

/**
 * VOICE BENCH — what a voice owns. Quiet by default: is read, to pick one.
 * `source material` here is the clips that fed the voice, not the original media.
 */
export function VoiceBench({
  voice,
  omitTakes = false,
}: {
  voice: Voice | undefined
  omitTakes?: boolean
}) {
  const runs = useQuery({
    queryKey: ['runs', voice?.id],
    queryFn: () => fetchRuns(voice?.id),
    enabled: Boolean(voice) && !omitTakes,
  })

  if (!voice) {
    return null
  }


  const references = referencesOf(voice)
  const clips = voiceClips(voice)
  const material =
    clips.length > 0
      ? clips.map((clip) => ({
          id: clip.id,
          label: clip.source_title ?? clip.id,
          duration_s: clip.ranges.reduce((total, range) => total + (range.end_s - range.start_s), 0),
        }))
      : (voice.sources ?? []).map((item) => ({
          id: item.id,
          label: item.label || item.id,
          duration_s: item.duration_s ?? 0,
        }))
  const derivatives = derivativesOf(voice)
  const takes = runs.data ?? []

  return (
    <div className="flex flex-col gap-2">
      {omitTakes ? null : (
        <h2 className="text-sm font-semibold text-zinc-50">Voice bench · {voice.name}</h2>
      )}
      <BenchSection
        title="REFERENCES"
        hint="Approved material Breeze may generate against. ★ is the active reference."
        empty={references.length === 0}
      >
        <ul className="mt-2 flex flex-col gap-2">
          {references.map((artifact) => (
            <li className="flex flex-col gap-1" key={artifact.id}>
              <span className="text-xs text-zinc-200">
                {artifact.default || artifact.id === voice.default_reference_id ? '★ ' : ''}
                {artifact.name ?? kindWord(artifact.kind)}
                {artifact.duration_s == null ? null : ` · ${formatSeconds(artifact.duration_s)}`}
                {artifact.default && isCroppedReference(voice) ? (
                  <span className="text-zinc-500"> of {formatSeconds(sourceDurationS(voice))}</span>
                ) : null}
                {artifact.stale ? <span className="text-amber-200"> · stale</span> : null}
              </span>
              <AudioBar src={artifactAudioUrl(artifact.audio_artifact_id)} label="" />
            </li>
          ))}
        </ul>
      </BenchSection>

      <BenchSection
        title="SOURCE MATERIAL"
        hint="Clips cropped out of sources. One voice may collect clips from many sources."
        empty={material.length === 0}
      >
        <ul className="mt-2 flex flex-col gap-1">
          {material.map((clip) => (
            <li className="flex items-center justify-between gap-2 text-xs text-zinc-200" key={clip.id}>
              <span className="truncate">{clip.label}</span>
              <span className="shrink-0 text-zinc-500">{formatSeconds(clip.duration_s)}</span>
            </li>
          ))}
        </ul>
      </BenchSection>

      <BenchSection
        title="DERIVATIVES"
        hint="A processed render of a reference or a clip. Reversible: the reference stays."
        empty={derivatives.length === 0}
      >
        <ul className="mt-2 flex flex-col gap-2">
          {derivatives.map((artifact) => (
            <li className="flex flex-col gap-1" key={artifact.id}>
              <span
                className={
                  artifact.stale ? 'text-xs text-amber-200' : 'text-xs text-zinc-200'
                }
              >
                {artifact.name ?? kindWord(artifact.kind)}
                {artifact.duration_s == null ? null : ` · ${formatSeconds(artifact.duration_s)}`}
              </span>
              <AudioBar src={artifactAudioUrl(artifact.audio_artifact_id)} label="" />
            </li>
          ))}
        </ul>
      </BenchSection>
      {omitTakes ? null : (
        <BenchSection
          title="TAKES"
          hint="Breeze generated audio recorded against this voice."
          empty={takes.length === 0}
        >
          <ul className="mt-2 flex flex-col gap-2">
            {takes.map((take) => (
              <li className="flex flex-col gap-1" key={take.id}>
                <span className="text-xs text-zinc-200">
                  take {take.id} · {formatSeconds(take.duration_s)} · {formatMs(take.first_audio_ms)}{' '}
                  first audio
                  {take.created_at ? (
                    <span className="text-zinc-500"> · {formatClock(take.created_at)}</span>
                  ) : null}
                </span>
                <AudioBar src={artifactAudioUrl(take.output_artifact_id)} label="" />
              </li>
            ))}
          </ul>
        </BenchSection>
      )}
    </div>
  )
}
