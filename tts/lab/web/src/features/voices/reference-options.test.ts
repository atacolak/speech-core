import { describe, expect, it } from 'vitest'
import type { Voice } from '@/lib/api'
import { type ReferenceClip, referenceOptions } from '@/features/voices/reference-options'

/** A voice profile, shaped the way `/api/voices` serves one. */
function voice(overrides: Partial<Voice> = {}): Voice {
  return {
    id: 'vp1',
    name: 'ata',
    tags: [],
    source_audio_artifact_id: 'art_src',
    source_transcript: 'legacy A words',
    keep_intervals: [{ start_s: 0, end_s: 11.5 }],
    effective_transcript: 'legacy A words',
    active_reference_variant_id: null,
    latest_take_id: 'run_1',
    created_at: '2026-09-16T00:00:00Z',
    updated_at: '2026-09-16T00:00:00Z',
    ...overrides,
  }
}

function withClips(profile: Voice, clips: ReferenceClip[]): Voice {
  return { ...profile, clips } as Voice
}

const ORIGINAL_VARIANT = {
  id: 'rv_orig',
  voice_profile_id: 'vp1',
  kind: 'original',
  audio_artifact_id: 'art_src',
  duration_s: 11.5,
}

describe('referenceOptions', () => {
  it('offers the enrolled reference artifacts with their own transcripts and never a run', () => {
    const profile = withClips(
      voice({
        artifacts: [
          {
            id: 'ref_a',
            role: 'reference',
            kind: 'original',
            name: 'take A',
            audio_artifact_id: 'art_src',
          },
          {
            id: 'ref_b',
            role: 'reference',
            kind: 'original',
            name: 'clip B',
            audio_artifact_id: 'art_clip_b',
          },
        ],
        default_reference_id: 'ref_b',
      }),
      [{ id: 'clip_b', audio_artifact_id: 'art_clip_b', clean_transcript: 'clip B clean words' }],
    )

    const options = referenceOptions(profile)

    expect(options.map((item) => item.id)).not.toContain(profile.latest_take_id)
    expect(options.map((item) => item.id)).toEqual(['ref_b', 'ref_a'])
    expect(options.find((item) => item.id === 'ref_b')).toMatchObject({
      target: 'ref_b',
      audioArtifactId: 'art_clip_b',
      selected: true,
      transcript: 'clip B clean words',
      isOriginal: true,
    })
    expect(options.find((item) => item.id === 'ref_a')).toMatchObject({
      selected: false,
      isOriginal: true,
      transcript: 'legacy A words',
    })
  })

  it('resolves a declared source transcript and never borrows another origin text', () => {
    const profile = voice({
      sources: [
        {
          id: 'src_one',
          label: 'take one',
          artifact_id: 'art_one',
          transcript: 'source one words',
          duration_s: 4,
        },
        { id: 'src_two', label: 'take two', artifact_id: 'art_two', transcript: '', duration_s: 4 },
      ],
      artifacts: [
        {
          id: 'ref_one',
          role: 'reference',
          kind: 'original',
          audio_artifact_id: 'art_one',
          source_id: 'src_one',
        },
        {
          id: 'ref_two',
          role: 'reference',
          kind: 'resemble',
          audio_artifact_id: 'art_two',
          source_id: 'src_two',
        },
      ],
    })

    const options = referenceOptions(profile)

    expect(options.find((item) => item.id === 'ref_one')?.transcript).toBe('source one words')
    expect(options.find((item) => item.id === 'ref_two')?.transcript).toBe('')
  })

  it('puts the legacy original ahead of a derived reference when nothing is stored', () => {
    const profile = voice({
      variants: [
        {
          id: 'rv_den',
          voice_profile_id: 'vp1',
          kind: 'resemble',
          audio_artifact_id: 'art_den',
          duration_s: 11.5,
        },
        ORIGINAL_VARIANT,
      ],
    })

    const options = referenceOptions(profile)

    expect(options.map((item) => item.id)).toEqual(['rv_orig', 'rv_den'])
    expect(options[0]).toMatchObject({
      isOriginal: true,
      label: '★ original',
      selected: true,
    })
    expect(options[1]).toMatchObject({ isOriginal: false, label: 'denoised', selected: false })
  })

  it('names the stored selection first by a neutral word and flags a stale render', () => {
    const profile = voice({
      active_reference_variant_id: 'rv_den',
      variants: [
        ORIGINAL_VARIANT,
        {
          id: 'rv_den',
          voice_profile_id: 'vp1',
          kind: 'resemble',
          audio_artifact_id: 'art_den',
          duration_s: 11.5,
          stale: true,
        },
        {
          id: 'rv_auk',
          voice_profile_id: 'vp1',
          kind: 'auk',
          audio_artifact_id: 'art_auk',
          duration_s: 11.5,
          auk_task: 'enhance',
        },
      ],
    })

    const options = referenceOptions(profile)

    expect(options.map((item) => item.id)).toEqual(['rv_den', 'rv_orig', 'rv_auk'])
    expect(options[0]).toMatchObject({
      selected: true,
      stale: true,
      label: '★ denoised (stale)',
      audioArtifactId: 'art_den',
      transcript: 'legacy A words',
    })
    expect(options.map((item) => item.label).join(' ')).not.toMatch(/resemble|enhance|auk/i)
  })

  it('falls back to the voice source when it has no reference row', () => {
    const options = referenceOptions(voice({ variants: [] }))

    expect(options).toEqual([
      {
        id: 'art_src',
        target: 'art_src',
        label: '★ original',
        selected: true,
        isOriginal: true,
        transcript: 'legacy A words',
        audioArtifactId: 'art_src',
        stale: false,
      },
    ])
    expect(referenceOptions(undefined)).toEqual([])
  })
})
