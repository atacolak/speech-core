import { describe, expect, it } from 'vitest'
import type { RunItem, Voice, VoiceArtifact, VoiceSource } from '@/lib/api'
import { voiceAssets, voiceGenerations } from '@/features/voices/voice-assets'

/** An AuK experiment: lineage-bearing, an experiment until the operator approves it. */
function aukArtifact(overrides: Record<string, unknown> = {}) {
  return {
    id: 'rv_auk',
    voice_profile_id: 'vp1',
    kind: 'auk',
    audio_artifact_id: 'art_auk',
    duration_s: 12,
    auk_task: 'enhance',
    instruction: 'Remove noise and reverberation while retaining the speech.',
    auk_precision: 'bf16',
    seed: 7,
    approved: false,
    stale: false,
    ...overrides,
  }
}

/** Today's 1:1 voice JSON: one source, variants, one active reference. */
function legacyVoice(overrides: Record<string, unknown> = {}): Voice {
  return {
    id: 'vp1',
    name: 'ata',
    tags: [],
    source_audio_artifact_id: 'art_src',
    original_artifact_id: 'art_src',
    original_format: 'wav',
    source_transcript: 'hello',
    keep_intervals: [{ start_s: 2, end_s: 6 }],
    effective_transcript: 'hello',
    active_reference_variant_id: 'rv_a2',
    variants: [],
    duration_s: 12,
    source_duration_s: 12,
    effective_duration_s: 4,
    latest_take_id: null,
    created_at: '2026-09-12T00:00:00Z',
    updated_at: '2026-09-12T00:00:00Z',
    ...overrides,
  } as Voice
}

/** The profile shape t1 emits: many sources, role-carrying artifacts, one optional default. */
function profileVoice(overrides: Record<string, unknown> = {}): Voice {
  return legacyVoice({
    sources: [
      {
        id: 'src_a',
        label: 'take A',
        artifact_id: 'art_src',
        transcript: 'hello',
        duration_s: 12,
        keep_intervals: [{ start_s: 2, end_s: 6 }],
      },
      {
        id: 'src_b',
        label: 'take B',
        artifact_id: 'art_src_b',
        transcript: 'hello again',
        duration_s: 9,
        keep_intervals: [{ start_s: 0, end_s: 9 }],
      },
    ],
    artifacts: [
      {
        id: 'a_exp',
        role: 'experiment',
        kind: 'auk',
        name: 'enhance 1',
        audio_artifact_id: 'art_exp',
        source_id: 'src_a',
        auk_task: 'enhance',
        instruction: 'Remove noise and reverberation while retaining the speech.',
        approved: false,
        default: false,
        stale: false,
      },
      {
        id: 'a_ref',
        role: 'reference',
        kind: 'auk',
        name: 'enhance 1 approved',
        audio_artifact_id: 'art_ref',
        parent_id: 'a_exp',
        source_id: 'src_a',
        auk_task: 'enhance',
        instruction: 'Remove noise and reverberation while retaining the speech.',
        approved: true,
        default: true,
        stale: false,
      },
    ],
    default_reference_id: 'a_ref',
    ...overrides,
  })
}

function artifact(overrides: Partial<VoiceArtifact> & { id: string }): VoiceArtifact {
  return {
    role: 'experiment',
    kind: 'crop',
    audio_artifact_id: `art_${overrides.id}`,
    parent_id: null,
    source_id: null,
    approved: false,
    default: false,
    stale: false,
    ...overrides,
  }
}

const SOURCE_A: VoiceSource = {
  id: 'src_a',
  label: 'source take A',
  artifact_id: 'art_src_a',
  transcript: 'these violent delights have violent ends',
  duration_s: 12,
  keep_intervals: [{ start_s: 0, end_s: 8 }],
}

/**
 * One enrolled source, its crop, and what the crop rendered. The payload lists
 * the grandchildren first, so nesting has to follow parent ids.
 */
function lineageVoice(overrides: Record<string, unknown> = {}): Voice {
  return profileVoice({
    active_reference_variant_id: null,
    sources: [SOURCE_A],
    artifacts: [
      artifact({
        id: 'a_ref',
        role: 'reference',
        kind: 'auk',
        name: 'enhance 1 approved',
        audio_artifact_id: 'art_ref',
        parent_id: 'a_crop',
        source_id: 'src_a',
        approved: true,
        default: true,
      }),
      artifact({
        id: 'a_res',
        kind: 'resemble',
        name: 'Resemble denoise',
        audio_artifact_id: 'art_den',
        parent_id: 'a_crop',
        source_id: 'src_a',
      }),
      artifact({
        id: 'a_crop',
        kind: 'crop',
        name: 'Crop 0.0–8.0s',
        audio_artifact_id: 'art_crop',
        parent_id: 'a_orig_a',
        source_id: 'src_a',
        keep_intervals: [{ start_s: 0, end_s: 8 }],
      }),
      artifact({
        id: 'a_orig_a',
        role: 'reference',
        kind: 'original',
        name: 'Original',
        audio_artifact_id: 'art_src_a',
        source_id: 'src_a',
        approved: true,
      }),
    ],
    default_reference_id: 'a_ref',
    ...overrides,
  })
}

const RUN: RunItem = {
  id: 'run_1',
  voice_id: 'vp1',
  output_artifact_id: 'art_take',
  latency_ms: 900,
  first_audio_ms: 120,
  duration_s: 6,
  rating: null,
  tags: [],
  created_at: '2026-09-14T00:00:00Z',
}

describe('voice assets', () => {
  it('derives one source from the legacy single-source voice', () => {
    const assets = voiceAssets(legacyVoice())
    expect(assets.sources).toHaveLength(1)
    expect(assets.sources[0]?.artifact_id).toBe('art_src')
    expect(assets.sources[0]?.keep_intervals).toEqual([{ start_s: 2, end_s: 6 }])
    expect(assets.sources[0]?.duration_s).toBe(12)
  })

  it('names the only source of a legacy voice as the primary', () => {
    expect(voiceAssets(legacyVoice()).primarySourceId).toBe('art_src')
  })

  it('names the primary source even when the payload lists it after another take', () => {
    const voice = profileVoice({ source_audio_artifact_id: 'art_src_b' })
    expect(voiceAssets(voice).primarySourceId).toBe('src_b')
  })

  it('reads the multi-source profile shape as its own sources', () => {
    const assets = voiceAssets(profileVoice())
    expect(assets.sources.map((item) => item.label)).toEqual(['take A', 'take B'])
  })

  it('lists one row per source, with the source audio the lab kept', () => {
    const assets = voiceAssets(lineageVoice())
    expect(assets.originals.map((row) => row.id)).toEqual([
      'src_a',
      // The `original` artifact is the source's own lineage root: it renders as the source row.
      'a_ref',
      'a_res',
      'a_crop',
    ])
    expect(assets.roots.map((row) => row.id)).toEqual(['src_a'])
    expect(assets.originals[0]?.audioArtifactId).toBe('art_src_a')
    expect(assets.originals[0]?.label).toBe('source take A')
    expect(assets.originals[0]?.transcript).toBe('these violent delights have violent ends')
  })

  it('nests every derived original under the parent its id names', () => {
    const assets = voiceAssets(lineageVoice())
    expect(assets.childrenByParent.get('src_a')?.map((row) => row.id)).toEqual(['a_crop'])
    // The payload lists a_ref and a_res first; their parent is still the crop.
    expect(assets.childrenByParent.get('a_crop')?.map((row) => row.id)).toEqual(['a_ref', 'a_res'])
    expect(assets.childrenByParent.get('a_ref')).toBeUndefined()
  })

  it('labels a derived row with the parent it came from, resolved by id', () => {
    const assets = voiceAssets(lineageVoice())
    const crop = assets.childrenByParent.get('src_a')?.[0]
    expect(crop?.derivedFrom).toBe('source take A')
    const approved = assets.childrenByParent.get('a_crop')?.[0]
    expect(approved?.derivedFrom).toBe('Crop 0.0–8.0s')
  })

  it('keeps every kind of artifact as an original, Resemble included', () => {
    const voice = profileVoice()
    voice.artifacts = [
      ...(voice.artifacts ?? []),
      artifact({ id: 'a_den', kind: 'resemble', name: 'Resemble', source_id: 'src_a' }),
      artifact({ id: 'a_vibe', kind: 'vibevoice', name: 'VibeVoice', source_id: 'src_a' }),
    ]
    const assets = voiceAssets(voice)
    expect(assets.originals.map((row) => row.id)).toEqual([
      'src_a',
      'src_b',
      'a_exp',
      'a_ref',
      'a_den',
      'a_vibe',
    ])
  })

  it('keeps a legacy resemble variant in the tree instead of parking it', () => {
    const assets = voiceAssets(
      legacyVoice({
        variants: [
          aukArtifact(),
          { id: 'rv_den', kind: 'resemble', audio_artifact_id: 'art_den', duration_s: 11.5 },
        ],
      }),
    )
    // Parentless legacy variants rendered from the voice hang off its primary source.
    expect(assets.childrenByParent.get('art_src')?.map((row) => row.id)).toEqual([
      'rv_auk',
      'rv_den',
    ])
    // The retiring VoiceWorkbench projection still parks every non-AuK kind (Task 10 deletes it).
    expect(assets.experiments.map((item) => item.id)).toEqual(['rv_auk'])
    expect(assets.references).toEqual([])
  })

  it('reads a legacy original variant as the source selection, not a child', () => {
    const assets = voiceAssets(
      legacyVoice({
        active_reference_variant_id: 'rv_ford',
        variants: [
          {
            id: 'rv_ford',
            voice_profile_id: 'vp1',
            kind: 'original',
            audio_artifact_id: 'art_src',
            duration_s: 4,
          },
        ],
      }),
    )
    expect(assets.originals.map((row) => row.id)).toEqual(['art_src'])
    expect(assets.originals[0]?.reference).toBe(true)
  })

  it('rests the star on the one stored reference', () => {
    const assets = voiceAssets(lineageVoice())
    expect(assets.defaultReferenceId).toBe('a_ref')
    expect(assets.originals.filter((row) => row.reference).map((row) => row.id)).toEqual(['a_ref'])
  })

  it('falls back to the artifact default flag when the profile carries no default id', () => {
    expect(voiceAssets(profileVoice({ default_reference_id: null })).defaultReferenceId).toBe('a_ref')
  })

  it('ignores a default id that names something other than a reference', () => {
    const voice = lineageVoice({ default_reference_id: 'a_crop' })
    voice.artifacts = (voice.artifacts ?? []).map((item) => ({ ...item, default: false }))
    const assets = voiceAssets(voice)
    expect(assets.defaultReferenceId).toBeNull()
    expect(assets.originals.some((row) => row.reference)).toBe(false)
  })

  it('reports no default when the legacy voice has no approved reference', () => {
    const assets = voiceAssets(legacyVoice({ variants: [aukArtifact()] }))
    expect(assets.originals.some((row) => row.reference)).toBe(false)
    expect(assets.defaultReferenceId).toBeNull()
  })

  it('parks a resemble artifact out of the retiring workbench lists', () => {
    const voice = profileVoice()
    voice.artifacts = [
      ...(voice.artifacts ?? []),
      artifact({ id: 'a_den', kind: 'resemble', name: 'Resemble', source_id: 'src_a' }),
    ]
    const assets = voiceAssets(voice)
    expect([...assets.experiments, ...assets.references].map((item) => item.id)).toEqual([
      'a_exp',
      'a_ref',
    ])
  })

  it('keeps runs out of the originals tree', () => {
    const assets = voiceAssets(lineageVoice())
    expect(assets.originals.map((row) => row.id)).not.toContain('run_1')
    expect(assets.originals.map((row) => row.audioArtifactId)).not.toContain('art_take')
  })

  it('keeps a named take artifact out of the originals tree', () => {
    const base = lineageVoice()
    const assets = voiceAssets({
      ...base,
      artifacts: [
        ...(base.artifacts ?? []),
        artifact({
          id: 'vt_run_1',
          role: 'reference',
          kind: 'take',
          name: 'morning take',
          audio_artifact_id: 'art_take',
          instruction: 'produced take words',
        }),
      ],
    })
    expect(assets.originals.map((row) => row.id)).not.toContain('vt_run_1')
    expect(assets.originals.map((row) => row.label)).not.toContain('morning take')
  })
})

describe('voice generations', () => {
  it('names a titled take by its run name and leaves untitled takes quiet', () => {
    expect(voiceGenerations([RUN])).toEqual([
      {
        id: 'run_1',
        label: 'Untitled',
        audioArtifactId: 'art_take',
        durationS: 6,
        firstAudioMs: 120,
        createdAt: '2026-09-14T00:00:00Z',
      },
    ])
    expect(voiceGenerations([{ ...RUN, name: 'morning take' }])[0]?.label).toBe('morning take')
  })

  it('carries no reference marker for a take to wear', () => {
    expect(Object.keys(voiceGenerations([RUN])[0] ?? {})).not.toContain('reference')
  })

  it('reads a run the lab left open without inventing a length or a date', () => {
    expect(
      voiceGenerations([{ ...RUN, duration_s: null, first_audio_ms: null, created_at: undefined }]),
    ).toEqual([
      {
        id: 'run_1',
        label: 'Untitled',
        audioArtifactId: 'art_take',
        durationS: null,
        firstAudioMs: null,
        createdAt: null,
      },
    ])
  })
})
