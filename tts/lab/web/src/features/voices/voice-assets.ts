import {
  type ReferenceVariant,
  type RunItem,
  type Voice,
  type VoiceArtifact,
  type VoiceSource,
  keepDurationS,
  referenceWord,
  sourceDurationS,
} from '@/lib/api'
import { aukTaskWord } from '@/lib/auk-tasks'

/** Every source owns one `original` artifact: the lineage root its children point at. */
const ORIGINAL_KIND = 'original'

/** AuK is the retiring VoiceWorkbench's processor. Task 10 deletes that surface. */
const WORKBENCH_KIND = 'auk'

/** One ORIGINALS row: the voice's own material, or something the lab derived from it. */
export type OriginalRow = {
  /** A voice source id, or the artifact id of a derived row. */
  id: string
  /** The parent this row came from, named once by id; null for the voice's own material. */
  derivedFrom: string | null
  label: string
  audioArtifactId: string
  /** The row's own length when the lab declares one; never invented. */
  durationS: number | null
  /** The transcript Breeze would be conditioned on. Only the voice's own material has one. */
  transcript: string | null
  /** ★ — the one stored reference. Takes are not rows here, so none can wear it. */
  reference: boolean
  stale: boolean
}

/** A Breeze take. An unnamed generation cannot wear the ★; a named take enrolls elsewhere. */
export type GenerationRow = {
  id: string
  label: string
  audioArtifactId: string
  durationS: number | null
  firstAudioMs: number | null
  createdAt: string | null
}

/** The workbench's view of a voice profile, whichever shape the lab serves. */
export type VoiceAssets = {
  sources: VoiceSource[]
  /** The source the voice-level transcript, keep and 1:1 columns describe. */
  primarySourceId: string | null
  /** The voice's own sources first, then every artifact it derived, in payload order. */
  originals: OriginalRow[]
  /** Direct children by row id, in payload order. */
  childrenByParent: Map<string, OriginalRow[]>
  /** The rows with no parent on this voice: the ORIGINALS tree's roots, in payload order. */
  roots: OriginalRow[]
  defaultReferenceId: string | null
  /** Retiring VoiceWorkbench projections beside the Ontology: Task 10 deletes that surface. */
  experiments: VoiceArtifact[]
  references: VoiceArtifact[]
}

/** Profile artifacts declare no length of their own; a legacy variant still knows its own. */
type DatedArtifact = VoiceArtifact & { duration_s?: number | null }

const NO_ASSETS: VoiceAssets = {
  sources: [],
  primarySourceId: null,
  originals: [],
  childrenByParent: new Map(),
  roots: [],
  defaultReferenceId: null,
  experiments: [],
  references: [],
}

/**
 * Legacy variants the lab rendered from the voice: any kind is an original here,
 * an experiment until the operator approves it. Resemble stays visible.
 */
function artifactFromVariant(variant: ReferenceVariant): DatedArtifact {
  return {
    id: variant.id,
    role: variant.approved ? 'reference' : 'experiment',
    kind: variant.kind,
    name: variant.kind === WORKBENCH_KIND ? aukTaskWord(variant.auk_task) : null,
    audio_artifact_id: variant.audio_artifact_id,
    parent_id: variant.parent_variant_id ?? null,
    source_id: null,
    duration_s: variant.duration_s ?? null,
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

/** The GENERATIONS pile: Breeze takes, in the order the lab lists them. */
export function voiceGenerations(runs: RunItem[]): GenerationRow[] {
  return runs.map((run) => ({
    id: run.id,
    label: run.name?.trim() || 'Untitled',
    audioArtifactId: run.output_artifact_id,
    durationS: run.duration_s ?? null,
    firstAudioMs: run.first_audio_ms ?? null,
    createdAt: run.created_at ?? null,
  }))
}

/** Sources and every derived artifact as one ORIGINALS tree, plus the one stored ★. */
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
  const artifacts: DatedArtifact[] = voice.artifacts?.length
    ? voice.artifacts
    : (voice.variants ?? []).map(artifactFromVariant)
  // The primary source is the one the voice's 1:1 columns describe (backend `primary_source_row`).
  const primary =
    sources.find((item) => item.artifact_id === voice.source_audio_artifact_id) ?? sources[0]
  const primarySourceId = primary?.id ?? null

  // A source's `original` artifact is the row the source already is: same audio, never a second row.
  const rootRowByArtifactId = new Map<string, string>()
  for (const source of sources) {
    const root = artifacts.find(
      (item) => item.kind === ORIGINAL_KIND && item.audio_artifact_id === source.artifact_id,
    )
    if (root) {
      rootRowByArtifactId.set(root.id, source.id)
    }
  }
  const absorbedIds = new Set(rootRowByArtifactId.keys())
  const sourceIds = new Set(sources.map((item) => item.id))

  // Row labels, resolved once by id, so a child can name the parent its id points at.
  const labels = new Map<string, string>()
  for (const source of sources) {
    labels.set(source.id, source.label || source.id)
  }
  for (const item of artifacts) {
    labels.set(item.id, item.name?.trim() || referenceWord(item.kind))
  }
  for (const [artifactId, rowId] of rootRowByArtifactId) {
    labels.set(artifactId, labels.get(rowId) ?? rowId)
  }

  // The backend files an artifact with no source of its own under the primary source.
  const parentRowId = (item: DatedArtifact): string | null => {
    if (item.parent_id) {
      return rootRowByArtifactId.get(item.parent_id) ?? item.parent_id
    }
    if (item.source_id && sourceIds.has(item.source_id)) {
      return item.source_id
    }
    return primarySourceId
  }

  const rowId = (artifactId: string) => rootRowByArtifactId.get(artifactId) ?? artifactId
  const capable = new Set(sources.map((item) => item.id))
  for (const item of artifacts) {
    if (item.role === 'reference') {
      capable.add(rowId(item.id))
    }
  }
  const wanted = voice.default_reference_id ?? voice.active_reference_variant_id ?? null
  const variant = wanted ? (voice.variants ?? []).find((item) => item.id === wanted) : undefined
  // A legacy `original` variant is the source's own selection, never a derived child.
  const ownSource =
    variant?.kind === ORIGINAL_KIND
      ? sources.find((item) => item.artifact_id === variant.audio_artifact_id)
      : undefined
  const flagged = artifacts.find((item) => item.role === 'reference' && item.default)
  const defaultReferenceId =
    (wanted && capable.has(wanted) ? rowId(wanted) : null) ??
    ownSource?.id ??
    (flagged ? rowId(flagged.id) : null) ??
    null

  const pending = [
    ...sources.map((source) => ({
      parentId: null as string | null,
      row: {
        id: source.id,
        derivedFrom: null,
        label: labels.get(source.id) ?? source.id,
        audioArtifactId: source.artifact_id,
        durationS: source.duration_s ?? null,
        transcript: (source.transcript ?? '').trim() || null,
        reference: false,
        stale: false,
      } satisfies OriginalRow,
    })),
    ...artifacts
      .filter((item) => !absorbedIds.has(item.id) && item.kind !== 'take')
      .map((item) => {
        const parent = parentRowId(item)
        return {
          parentId: parent,
          row: {
            id: item.id,
            derivedFrom: parent ? (labels.get(parent) ?? parent) : null,
            label: labels.get(item.id) ?? item.id,
            audioArtifactId: item.audio_artifact_id,
            durationS:
              item.duration_s ?? (item.keep_intervals?.length ? keepDurationS(item.keep_intervals) : null),
            transcript: null,
            reference: false,
            stale: Boolean(item.stale),
          } satisfies OriginalRow,
        }
      }),
  ]
  const rowIds = new Set(pending.map((entry) => entry.row.id))
  const parentByRowId = new Map(pending.map((entry) => [entry.row.id, entry.parentId]))
  const originals = pending.map((entry) => ({
    ...entry.row,
    reference: entry.row.id === defaultReferenceId,
  }))
  const childrenByParent = new Map<string, OriginalRow[]>()
  const roots: OriginalRow[] = []
  for (const row of originals) {
    const parent = parentByRowId.get(row.id) ?? null
    // A parent this voice no longer holds would hide the row: surface it as a root instead.
    if (parent === null || !rowIds.has(parent)) {
      roots.push(row)
      continue
    }
    const siblings = childrenByParent.get(parent)
    if (siblings) {
      siblings.push(row)
    } else {
      childrenByParent.set(parent, [row])
    }
  }

  const workbench = artifacts.filter((item) => item.kind === WORKBENCH_KIND)
  return {
    sources,
    primarySourceId,
    originals,
    childrenByParent,
    roots,
    defaultReferenceId,
    experiments: workbench.filter((item) => item.role !== 'reference'),
    references: workbench.filter((item) => item.role === 'reference'),
  }
}
