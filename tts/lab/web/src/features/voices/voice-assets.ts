import {
  type ReferenceVariant,
  type Voice,
  type VoiceArtifact,
  type VoiceSource,
  sourceDurationS,
} from '@/lib/api'
import { aukTaskWord } from '@/lib/auk-tasks'

/** The workbench's view of a voice profile, whichever shape the lab serves. */
export type VoiceAssets = {
  sources: VoiceSource[]
  /** The source the voice-level transcript, keep and 1:1 columns describe. */
  primarySourceId: string | null
  experiments: VoiceArtifact[]
  references: VoiceArtifact[]
  defaultReferenceId: string | null
}

/** AuK is the workbench's processor. Specialist kinds stay in the API, parked from the bench. */
const WORKBENCH_KIND = 'auk'

const NO_ASSETS: VoiceAssets = {
  sources: [],
  primarySourceId: null,
  experiments: [],
  references: [],
  defaultReferenceId: null,
}

/**
 * Specialist variants (resemble, …) stay in the API but are not workbench assets.
 * AuK candidates carry their own lineage; approval is what makes one a reference.
 */
function artifactFromVariant(variant: ReferenceVariant): VoiceArtifact | null {
  if (variant.kind !== WORKBENCH_KIND) {
    return null
  }
  return {
    id: variant.id,
    role: variant.approved ? 'reference' : 'experiment',
    kind: variant.kind,
    name: aukTaskWord(variant.auk_task),
    audio_artifact_id: variant.audio_artifact_id,
    parent_id: variant.parent_variant_id ?? null,
    source_id: null,
    auk_task: variant.auk_task ?? null,
    instruction: variant.instruction ?? null,
    model_variant: variant.model_variant ?? null,
    auk_precision: variant.auk_precision ?? null,
    seed: variant.seed ?? null,
    settings: variant.settings ?? null,
    approved: Boolean(variant.approved),
    default: false,
    stale: Boolean(variant.stale),
  }
}

/** Sources, experiments, references and the ★ default — additive over the legacy JSON. */
export function voiceAssets(voice: Voice | undefined): VoiceAssets {
  if (!voice) {
    return NO_ASSETS
  }
  let sources = voice.sources ?? []
  if (sources.length === 0 && voice.source_audio_artifact_id) {
    // Today's 1:1 voice is one source: the original take plus its keep crop.
    sources = [
      {
        id: voice.source_audio_artifact_id,
        label: 'source',
        artifact_id: voice.source_audio_artifact_id,
        transcript: voice.source_transcript,
        duration_s: sourceDurationS(voice),
        keep_intervals: voice.keep_intervals,
      },
    ]
  }
  const artifacts = (
    voice.artifacts ??
    (voice.variants ?? []).flatMap((variant) => {
      const artifact = artifactFromVariant(variant)
      return artifact ? [artifact] : []
    })
  ).filter((item) => item.kind === WORKBENCH_KIND)
  const references = artifacts.filter((item) => item.role === 'reference')
  // The primary source is the one the voice's 1:1 columns describe (backend `primary_source_row`).
  const primary =
    sources.find((item) => item.artifact_id === voice.source_audio_artifact_id) ?? sources[0]
  // The legacy world has one active reference; in the profile world an explicit default wins.
  const wanted = voice.default_reference_id ?? voice.active_reference_variant_id ?? null
  const named = wanted ? references.find((item) => item.id === wanted) : undefined
  return {
    sources,
    primarySourceId: primary?.id ?? null,
    experiments: artifacts.filter((item) => item.role !== 'reference'),
    references,
    defaultReferenceId: named?.id ?? references.find((item) => item.default)?.id ?? null,
  }
}
