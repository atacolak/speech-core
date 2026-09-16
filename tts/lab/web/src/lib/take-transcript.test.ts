import { describe, expect, it } from 'vitest'
import type { RunItem } from '@/lib/api'
import { takeTranscript } from '@/lib/take-transcript'

function run(snapshot: RunItem['request_snapshot']): RunItem {
  return {
    id: 'run_1',
    voice_id: 'vp_1',
    request_snapshot: snapshot,
    output_artifact_id: 'art_1',
    latency_ms: 180,
    first_audio_ms: 40,
    duration_s: 1.2,
    rating: null,
    tags: [],
  }
}

describe('takeTranscript', () => {
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
    expect(stoppedAfterTheLastSegment).toEqual({ text: 'Alpha. Beta.', truncated: false })
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
