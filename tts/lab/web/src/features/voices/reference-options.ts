import { type Voice, referenceWord } from '@/lib/api'

/** The backend attaches clips to the voice profile; the shared `Voice` type has not caught up. */
export type ReferenceClip = {
  id: string
  audio_artifact_id: string
  clean_transcript?: string | null
}

/** One row of the GENERATE reference picker. A run/take is never one. */
export type ReferenceOption = {
  /** The option's identity: the stored reference id it matches, and its React key. */
  id: string
  /** The id `POST /reference/activate` receives. */
  target: string
  label: string
  selected: boolean
  /** An ORIGINAL (enrolled source or clip). ★ marks an original or a reference, never a generation. */
  isOriginal: boolean
  /** The selected origin's own clean transcript, never another origin's text. */
  transcript: string
  audioArtifactId: string
  stale: boolean
}

type Candidate = {
  id: string
  kind: string
  name?: string | null
  audioArtifactId: string
  /** Set when the candidate is an artifact, so its declared source can win. */
  sourceId?: string | null
  stale: boolean
}

/**
 * The material Breeze may be conditioned on, in picker order: the stored
 * selection first, then originals ahead of derived references.
 *
 * Profile voices list their enrolled reference artifacts; legacy voices list
 * their `reference_variants` rows, which the activation route accepts as-is.
 */
export function referenceOptions(voice: Voice | undefined): ReferenceOption[] {
  if (!voice) {
    return []
  }
  const candidates = referenceCandidates(voice)
  const selection =
    voice.default_reference_id ??
    voice.active_reference_variant_id ??
    primaryReferenceId(voice, candidates)
  const clips = (voice as Voice & { clips?: ReferenceClip[] }).clips ?? []
  return candidates
    .map((candidate) => {
      const selected = candidate.id === selection
      return {
        id: candidate.id,
        target: candidate.id,
        label: optionLabel(candidate, selected),
        selected,
        isOriginal: candidate.kind === 'original',
        transcript: resolvedTranscript(voice, clips, candidate),
        audioArtifactId: candidate.audioArtifactId,
        stale: candidate.stale,
      }
    })
    .sort(
      (left, right) =>
        Number(right.selected) - Number(left.selected) ||
        Number(right.isOriginal) - Number(left.isOriginal),
    )
}

function referenceCandidates(voice: Voice): Candidate[] {
  // Profile world: approval is what makes a row a reference, so experiments stay out.
  const references = (voice.artifacts ?? []).filter((item) => item.role === 'reference')
  if (references.length > 0) {
    return references.map((item) => ({
      id: item.id,
      kind: item.kind,
      name: item.name,
      audioArtifactId: item.audio_artifact_id,
      sourceId: item.source_id ?? null,
      stale: Boolean(item.stale),
    }))
  }
  const variants = voice.variants ?? []
  if (variants.length > 0) {
    return variants.map((item) => ({
      id: item.id,
      kind: item.kind,
      audioArtifactId: item.audio_artifact_id,
      stale: Boolean(item.stale),
    }))
  }
  if (!voice.source_audio_artifact_id) {
    return []
  }
  // A voice with no reference row still has its own source: the one original.
  return [
    {
      id: voice.active_reference_variant_id ?? voice.source_audio_artifact_id,
      kind: 'original',
      audioArtifactId: voice.source_audio_artifact_id,
      stale: false,
    },
  ]
}

/**
 * With no stored id the lab itself falls back to the primary source: its keep
 * crop is the audio and its transcript is the text, so that original is the
 * effective selection.
 */
function primaryReferenceId(voice: Voice, candidates: Candidate[]): string | null {
  const own = candidates.find((item) => item.audioArtifactId === voice.source_audio_artifact_id)
  return own?.id ?? voice.source_audio_artifact_id
}

/**
 * Mirrors `reference_transcript` in the lab: a selected artifact's declared
 * source wins, then a clip's clean transcript, then the legacy primary text.
 * An origin with no transcript shows empty, never another origin's words.
 */
function resolvedTranscript(voice: Voice, clips: ReferenceClip[], candidate: Candidate): string {
  if (candidate.sourceId) {
    const source = (voice.sources ?? []).find((item) => item.id === candidate.sourceId)
    if (source) {
      return (source.transcript ?? '').trim()
    }
  }
  const clip = clips.find(
    (item) => item.id === candidate.id || item.audio_artifact_id === candidate.audioArtifactId,
  )
  if (clip) {
    return (clip.clean_transcript ?? '').trim()
  }
  return (voice.effective_transcript || voice.source_transcript || '').trim()
}

/** The neutral word for the kind, plus the stored marks: ★ selected, (stale). */
function optionLabel(candidate: Candidate, selected: boolean): string {
  const name = (candidate.name ?? '').trim() || referenceWord(candidate.kind)
  return `${selected ? '★ ' : ''}${name}${candidate.stale ? ' (stale)' : ''}`
}
