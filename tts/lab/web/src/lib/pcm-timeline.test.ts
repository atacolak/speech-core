import { describe, expect, it } from 'vitest'
import { PcmTimeline, decodePcmWav } from '@/lib/pcm-timeline'

function pcm(...samples: number[]): ArrayBuffer {
  const values = new Int16Array(samples)
  return values.buffer.slice(0)
}

type WavOptions = {
  format?: number
  channels?: number
  bits?: number
  /** Write a LIST chunk before `data`, so `data` does not start at byte 44. */
  listChunk?: boolean
}

/** Minimal RIFF/WAVE writer for the decode contract. */
function wav(samples: number[], options: WavOptions = {}): ArrayBuffer {
  const { format = 1, channels = 1, bits = 16, listChunk = true } = options
  const data = new Int16Array(samples)
  const ascii = (bytes: Uint8Array, offset: number, text: string) => {
    for (let index = 0; index < text.length; index += 1) {
      bytes[offset + index] = text.charCodeAt(index)
    }
  }
  const bytes = new Uint8Array(44 + (listChunk ? 12 : 0) + data.byteLength)
  const view = new DataView(bytes.buffer)
  ascii(bytes, 0, 'RIFF')
  view.setUint32(4, bytes.byteLength - 8, true)
  ascii(bytes, 8, 'WAVE')
  ascii(bytes, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, format, true)
  view.setUint16(22, channels, true)
  view.setUint32(24, 24_000, true)
  view.setUint32(28, 24_000 * channels * (bits / 8), true)
  view.setUint16(32, channels * (bits / 8), true)
  view.setUint16(34, bits, true)
  let dataOffset = 36
  if (listChunk) {
    ascii(bytes, 36, 'LIST')
    view.setUint32(40, 4, true)
    ascii(bytes, 44, 'INFO')
    dataOffset = 48
  }
  ascii(bytes, dataOffset, 'data')
  view.setUint32(dataOffset + 4, data.byteLength, true)
  bytes.set(new Uint8Array(data.buffer), dataOffset + 8)
  return bytes.buffer
}

describe('PcmTimeline', () => {
  it('appends split s16 frames without losing a sample', () => {
    const bytes = new Uint8Array(pcm(-32768, 0, 32767))
    const split = new PcmTimeline(24_000)
    split.appendS16(bytes.slice(0, 1).buffer)
    split.appendS16(bytes.slice(1, 5).buffer)
    split.appendS16(bytes.slice(5).buffer)
    const combined = new PcmTimeline(24_000)
    combined.appendS16(bytes.buffer)
    expect([...split.toFloat32()]).toEqual([...combined.toFloat32()])
    expect(split.length).toBe(3)
  })

  it('grows duration monotonically and returns bounded peaks', () => {
    const timeline = new PcmTimeline(4)
    expect(timeline.appendS16(pcm(-32768, 0))).toBe(0.5)
    expect(timeline.appendS16(pcm(16384, 32767))).toBe(1)
    expect(timeline.peaks(2)).toEqual([
      { min: -1, max: 0 },
      { min: expect.closeTo(16384 / 32768), max: expect.closeTo(32767 / 32768) },
    ])
  })

  it('returns one bucket per sample when asked for more buckets than samples', () => {
    const timeline = new PcmTimeline(24_000)
    timeline.appendS16(pcm(-100, 200))
    expect(timeline.peaks(8)).toHaveLength(2)
  })

  it('copies samples out instead of exposing its internal store', () => {
    const timeline = new PcmTimeline(24_000)
    timeline.appendS16(pcm(1000, -1000))
    const copy = timeline.toFloat32()
    copy[0] = 0
    expect(timeline.toFloat32()[0]).toBeCloseTo(1000 / 32768)
  })
})

describe('decodePcmWav', () => {
  it('restores mono 16-bit PCM', () => {
    const decoded = decodePcmWav(wav([-32768, 0, 16384, 32767]))
    expect(decoded).not.toBeNull()
    expect(decoded?.sampleRate).toBe(24_000)
    expect(decoded?.durationS).toBeCloseTo(4 / 24_000)
    expect([...(decoded?.toFloat32() ?? [])]).toEqual([-1, 0, 16384 / 32768, 32767 / 32768])
  })

  it('returns null for malformed or non-PCM input', () => {
    expect(decodePcmWav(new ArrayBuffer(0))).toBeNull()
    expect(decodePcmWav(pcm(1, 2, 3))).toBeNull()
    expect(decodePcmWav(wav([1000, 2000], { format: 3 }))).toBeNull()
    expect(decodePcmWav(wav([1000, 2000], { channels: 2 }))).toBeNull()
    expect(decodePcmWav(wav([1000, 2000], { bits: 8 }))).toBeNull()
  })
})
