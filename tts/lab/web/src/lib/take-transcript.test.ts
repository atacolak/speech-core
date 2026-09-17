import { describe, expect, it } from 'vitest'
import type { RunItem } from '@/lib/api'
import { takeTranscript } from '@/lib/take-transcript'

function run(
  snapshot: RunItem['request_snapshot'],
  options: Partial<Pick<RunItem, 'duration_s' | 'alignment'>> = {},
): RunItem {
  return {
    id: 'run_1',
    voice_id: 'vp_1',
    request_snapshot: snapshot,
    output_artifact_id: 'art_1',
    latency_ms: 180,
    first_audio_ms: 40,
    duration_s: options.duration_s ?? 1.2,
    rating: null,
    tags: [],
    alignment: options.alignment,
  }
}

describe('takeTranscript', () => {
  it('uses produced_text instead of ready alignment words', () => {
    const result = takeTranscript(run({
      text: 'Requested words',
      produced_text: 'Produced words',
      segments_planned: 1,
      segments_completed: 1,
      stopped: false,
    }, {
      duration_s: 1.0,
      alignment: {
        status: 'ready',
        text: 'Wrong alignment',
        words: [
          { text: 'Wrong', start_s: 0, end_s: 0.3 },
          { text: 'alignment', start_s: 0.3, end_s: 0.6 },
        ],
      },
    }))
    expect(result).toEqual({ text: 'Produced words', truncated: false })
  })

  it('does not go blank when produced_text is empty but audio exists', () => {
    const result = takeTranscript(run({
      text: 'Alpha beta gamma plus many extra leftover words after that cut including delta',
      produced_text: '',
      segments_planned: 3,
      segments_completed: 0,
      stopped: true,
    }, { duration_s: 2.3 }))
    expect(result.text.length).toBeGreaterThan(1)
    expect(result.text.endsWith(' --')).toBe(true)
    expect(result.text.includes('delta')).toBe(false)
    expect(result.truncated).toBe(true)
  })

  it('does not show unborn Say when produced_text is missing on a stopped run', () => {
    const result = takeTranscript(run({
      text: 'One. Two. extra leftover words. Three.',
      segments_planned: 3,
      segments_completed: 1,
      stopped: true,
    }, { duration_s: 1.2 }))
    expect(result.text.includes('Three')).toBe(false)
    expect(result.truncated).toBe(true)
  })

  it('marks incomplete produced text with a double dash', () => {
    const result = takeTranscript(
      run({
        text: 'One. Two. Three.',
        produced_text: 'One. Two.',
        segments_planned: 3,
        segments_completed: 2,
        stopped: true,
      }),
    )
    expect(result).toEqual({ text: 'One. Two. --', truncated: true })
  })

  it('marks incomplete takes from segment counts', () => {
    const disconnected = takeTranscript(
      run({
        produced_text: 'Alpha. Beta.',
        text: 'Alpha. Beta. Gamma.',
        segments_planned: 3,
        segments_completed: 2,
        stopped: false,
      }),
    )
    expect(disconnected).toEqual({ text: 'Alpha. Beta. --', truncated: true })

    const stoppedAfterTheLastSegment = takeTranscript(
      run({
        text: 'Alpha. Beta.',
        produced_text: 'Alpha. Beta.',
        segments_planned: 2,
        segments_completed: 2,
        stopped: true,
      }),
    )
    expect(stoppedAfterTheLastSegment).toEqual({ text: 'Alpha. Beta.', truncated: false })
  })

  it('does not mark a complete take with a double dash', () => {
    const result = takeTranscript(run({
      text: 'One two three',
      produced_text: 'One two three',
      segments_planned: 1,
      segments_completed: 1,
      stopped: false,
    }, {
      duration_s: 1.5,
      alignment: {
        status: 'ready',
        text: 'One two three',
        words: [
          { text: 'One', start_s: 0, end_s: 0.3 },
          { text: 'two', start_s: 0.3, end_s: 0.6 },
          { text: 'three', start_s: 0.6, end_s: 0.95 },
        ],
      },
    }))
    expect(result).toEqual({ text: 'One two three', truncated: false })
  })

  it('clips an overlong produced_text on a stopped short take when alignment is missing', () => {
    const result = takeTranscript(run({
      text: 'Alpha beta gamma plus many extra leftover words after that cut including epsilon',
      produced_text: 'Alpha beta gamma plus many extra leftover words after that cut including epsilon',
      segments_planned: 3,
      segments_completed: 0,
      stopped: true,
    }, { duration_s: 2.3 }))
    expect(result.text.endsWith(' --')).toBe(true)
    expect(result.text.includes('epsilon')).toBe(false)
    expect(result.truncated).toBe(true)
  })

  it('falls back to the requested text for an old run with no produced prefix', () => {
    expect(takeTranscript(run({ text: 'One shot line.' }))).toEqual({
      text: 'One shot line.',
      truncated: false,
    })
    expect(takeTranscript(run({}))).toEqual({ text: '', truncated: false })
    expect(takeTranscript(run(undefined))).toEqual({ text: '', truncated: false })
  })

  it('never marks an untruncated take with an ellipsis', () => {
    const result = takeTranscript(
      run({
        text: 'All. Planned.',
        produced_text: 'All. Planned.',
        segments_planned: 2,
        segments_completed: 2,
        stopped: false,
      }),
    )
    expect(result.text).toBe('All. Planned.')
  })
})
