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
  it('clips ready alignment words to take duration', () => {
    const result = takeTranscript(run({
      text: 'One two three four',
      produced_text: 'One two three four',
      segments_planned: 1,
      segments_completed: 1,
      stopped: true,
    }, {
      duration_s: 1.0,
      alignment: {
        status: 'ready',
        text: 'One two three four',
        words: [
          { text: 'One', start_s: 0, end_s: 0.3 },
          { text: 'two', start_s: 0.3, end_s: 0.6 },
          { text: 'three', start_s: 0.6, end_s: 0.9 },
          { text: 'four', start_s: 1.1, end_s: 1.4 },
        ],
      },
    }))
    expect(result.text).toBe('One two three…')
    expect(result.truncated).toBe(true)
  })

  it('does not go blank when produced_text is empty but audio exists', () => {
    const result = takeTranscript(run({
      text: 'Alpha beta gamma delta',
      produced_text: '',
      segments_planned: 3,
      segments_completed: 0,
      stopped: true,
    }, { duration_s: 2.3 }))
    expect(result.text.length).toBeGreaterThan(1)
    expect(result.text.endsWith('…')).toBe(true)
    expect(result.text.includes('delta')).toBe(false)
    expect(result.truncated).toBe(true)
  })

  it('does not show unborn Say when produced_text is missing on a stopped run', () => {
    const result = takeTranscript(run({
      text: 'One. Two. Three.',
      segments_planned: 3,
      segments_completed: 1,
      stopped: true,
    }, { duration_s: 1.2 }))
    expect(result.text.includes('Three')).toBe(false)
    expect(result.truncated).toBe(true)
  })

  it('shows the exact produced prefix plus an ellipsis when a segment never started', () => {
    const result = takeTranscript(
      run({
        text: 'One. Two. Three.',
        produced_text: 'One. Two.',
        segments_planned: 3,
        segments_completed: 2,
        stopped: true,
      }),
    )
    expect(result).toEqual({ text: 'One. Two.…', truncated: true })
  })

  it('derives truncation from the segment counts, not the stopped flag', () => {
    const disconnected = takeTranscript(
      run({
        text: 'Alpha. Beta. Gamma.',
        produced_text: 'Alpha. Beta.',
        segments_planned: 3,
        segments_completed: 2,
        stopped: false,
      }),
    )
    expect(disconnected).toEqual({ text: 'Alpha. Beta.…', truncated: true })

    const stoppedAfterTheLastSegment = takeTranscript(
      run({
        text: 'Alpha. Beta.',
        produced_text: 'Alpha. Beta.',
        segments_planned: 2,
        segments_completed: 2,
        stopped: true,
      }),
    )
    expect(stoppedAfterTheLastSegment).toEqual({ text: 'Alpha. Beta.…', truncated: true })
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
