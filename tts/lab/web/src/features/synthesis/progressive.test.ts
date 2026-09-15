import { describe, expect, it, vi } from 'vitest'
import { nextPlayable, ProgressivePlayer } from './progressive'
import type { GenerateSegment } from '@/lib/api'

const segment = (index: number, state: GenerateSegment['state']): GenerateSegment => ({
  index,
  text: `segment ${index}`,
  state,
  duration_s: state === 'generated' ? 1 : null,
  audio_url: state === 'generated' ? `/segments/${index}` : null,
})

describe('progressive playback queue', () => {
  it('never skips a lower segment that is not generated', () => {
    expect(nextPlayable([segment(0, 'generated'), segment(1, 'generating')], [])).toBe(0)
    expect(nextPlayable([segment(0, 'generated'), segment(1, 'generating'), segment(2, 'generated')], [0])).toBeNull()
  })

  it('schedules decoded buffers on one gapless audio clock', async () => {
    const starts: number[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(new Uint8Array([0, 1, 2, 3]).buffer, { status: 200 })),
    )
    const context = {
      currentTime: 10,
      destination: {},
      decodeAudioData: vi
        .fn()
        .mockResolvedValueOnce({ duration: 1.25 })
        .mockResolvedValueOnce({ duration: 0.75 }),
      createBufferSource: () => ({
        buffer: null,
        connect: vi.fn(),
        start: (when: number) => starts.push(when),
        stop: vi.fn(),
        onended: null,
      }),
    }
    const player = new ProgressivePlayer(context as unknown as AudioContext)
    await player.enqueue(0, '/segments/0')
    await player.enqueue(1, '/segments/1')
    expect(starts).toEqual([10, 11.25])
  })
})
