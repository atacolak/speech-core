import { describe, expect, it } from 'vitest'
import type { Voice } from '@/lib/api'
import { voiceAssets } from '@/features/voices/voice-assets'

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

describe('voice assets', () => {
  it('derives one source from the legacy single-source voice', () => {
    const assets = voiceAssets(legacyVoice())
    expect(assets.sources).toHaveLength(1)
    expect(assets.sources[0]?.artifact_id).toBe('art_src')
    expect(assets.sources[0]?.keep_intervals).toEqual([{ start_s: 2, end_s: 6 }])
    expect(assets.sources[0]?.duration_s).toBe(12)
  })

  it('splits auk variants into experiments and approved auk variants into references', () => {
    const assets = voiceAssets(
      legacyVoice({
        variants: [
          aukArtifact(),
          aukArtifact({ id: 'rv_auk_ok', audio_artifact_id: 'art_ok', approved: true }),
        ],
        active_reference_variant_id: 'rv_auk_ok',
      }),
    )
    expect(assets.experiments.map((item) => item.id)).toEqual(['rv_auk'])
    expect(assets.references.map((item) => item.id)).toEqual(['rv_auk_ok'])
    expect(assets.defaultReferenceId).toBe('rv_auk_ok')
  })

  it('parks a resemble variant out of both asset lists', () => {
    const assets = voiceAssets(
      legacyVoice({
        variants: [
          aukArtifact(),
          { id: 'rv_den', kind: 'resemble', audio_artifact_id: 'art_den', duration_s: 11.5 },
        ],
      }),
    )
    const ids = [...assets.experiments, ...assets.references].map((item) => item.id)
    expect(ids).toEqual(['rv_auk'])
  })

  it('names the only source of a legacy voice as the primary', () => {
    expect(voiceAssets(legacyVoice()).primarySourceId).toBe('art_src')
  })

  it('names the primary source even when the payload lists it after another take', () => {
    const voice = profileVoice({ source_audio_artifact_id: 'art_src_b' })
    expect(voiceAssets(voice).primarySourceId).toBe('src_b')
  })

  it('parks a resemble artifact out of both asset lists', () => {
    const voice = profileVoice()
    voice.artifacts = [
      ...(voice.artifacts ?? []),
      {
        id: 'a_den',
        role: 'experiment',
        kind: 'resemble',
        name: 'Resemble',
        audio_artifact_id: 'art_den',
        source_id: 'src_a',
        approved: false,
        default: false,
        stale: false,
      },
    ]
    const assets = voiceAssets(voice)
    const ids = [...assets.experiments, ...assets.references].map((item) => item.id)
    expect(ids).toEqual(['a_exp', 'a_ref'])
  })

  it('reports no default when the legacy voice has no approved reference', () => {
    const assets = voiceAssets(legacyVoice({ variants: [aukArtifact()] }))
    expect(assets.references).toEqual([])
    expect(assets.defaultReferenceId).toBeNull()
  })

  it('reads the multi-source profile shape as its own lists', () => {
    const assets = voiceAssets(profileVoice())
    expect(assets.sources.map((item) => item.label)).toEqual(['take A', 'take B'])
    expect(assets.experiments.map((item) => item.id)).toEqual(['a_exp'])
    expect(assets.references.map((item) => item.id)).toEqual(['a_ref'])
    expect(assets.experiments[0]?.source_id).toBe('src_a')
    expect(assets.references[0]?.parent_id).toBe('a_exp')
  })

  it('falls back to the artifact default flag when the profile carries no default id', () => {
    const assets = voiceAssets(profileVoice({ default_reference_id: null }))
    expect(assets.defaultReferenceId).toBe('a_ref')
  })

  it('ignores a default id that names no reference', () => {
    const voice = profileVoice({ default_reference_id: 'a_exp' })
    voice.artifacts = (voice.artifacts ?? []).map((item) => ({ ...item, default: false }))
    expect(voiceAssets(voice).defaultReferenceId).toBeNull()
  })
})
