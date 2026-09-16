export type Peak = { min: number; max: number }

const S16_SCALE = 32768

/** Decode interleaved-mono s16le bytes, ignoring a trailing odd byte. */
export function decodeS16(bytes: ArrayBuffer): Float32Array {
  const frames = bytes.byteLength >> 1
  const samples = new Float32Array(frames)
  if (frames === 0) {
    return samples
  }
  const source = new Int16Array(bytes, 0, frames)
  for (let index = 0; index < frames; index += 1) {
    samples[index] = source[index] / S16_SCALE
  }
  return samples
}

/**
 * One growing mono PCM take.
 *
 * Chunks append in arrival order. A chunk boundary may split an s16 frame, so
 * at most one byte is carried until its partner arrives; the decoded store
 * never sees a partial frame. Playback reads a copy through `toFloat32`, which
 * is why growing the timeline cannot disturb an already-started source.
 */
export class PcmTimeline {
  readonly sampleRate: number
  private readonly chunks: Float32Array[] = []
  private carry: number | null = null
  private samples = 0

  constructor(sampleRate: number) {
    this.sampleRate = sampleRate
  }

  get length(): number {
    return this.samples
  }

  get durationS(): number {
    return this.samples / this.sampleRate
  }

  /** Append interleaved-mono s16le. Returns the new duration. */
  appendS16(bytes: ArrayBuffer): number {
    let incoming = bytes
    if (this.carry !== null) {
      const merged = new Uint8Array(bytes.byteLength + 1)
      merged[0] = this.carry
      merged.set(new Uint8Array(bytes), 1)
      this.carry = null
      incoming = merged.buffer
    }
    const frames = incoming.byteLength >> 1
    if (frames > 0) {
      this.chunks.push(decodeS16(incoming))
      this.samples += frames
    }
    if (incoming.byteLength % 2 === 1) {
      this.carry = new Uint8Array(incoming)[incoming.byteLength - 1]
    }
    return this.durationS
  }

  /** Copy out for playback. */
  toFloat32(): Float32Array {
    const samples = new Float32Array(this.samples)
    let offset = 0
    for (const chunk of this.chunks) {
      samples.set(chunk, offset)
      offset += chunk.length
    }
    return samples
  }

  /** Downsample to `buckets` min/max pairs for the waveform. */
  peaks(buckets: number): Peak[] {
    return this.peaksRange(0, this.durationS, buckets)
  }

  /**
   * Downsample only `[startS, stopS)` — the visible window, not the whole take.
   *
   * Seconds are clamped into the stored samples first, so a window over a
   * growing take costs the range it shows and never the audio behind it.
   */
  peaksRange(startS: number, stopS: number, buckets: number): Peak[] {
    const from = this.sampleIndex(startS)
    const to = this.sampleIndex(stopS)
    const count = Math.min(Math.max(Math.floor(buckets), 0), to - from)
    if (count === 0) {
      return []
    }
    const peaks: Peak[] = []
    let chunk = 0
    let chunkStart = 0
    for (let bucket = 0; bucket < count; bucket += 1) {
      const start = from + Math.floor((bucket * (to - from)) / count)
      const stop = from + Math.floor(((bucket + 1) * (to - from)) / count)
      let min = Infinity
      let max = -Infinity
      for (let index = start; index < stop; index += 1) {
        while (index - chunkStart >= this.chunks[chunk].length) {
          chunkStart += this.chunks[chunk].length
          chunk += 1
        }
        const value = this.chunks[chunk][index - chunkStart]
        if (value < min) {
          min = value
        }
        if (value > max) {
          max = value
        }
      }
      peaks.push({ min, max })
    }
    return peaks
  }

  /** Nearest stored sample for a wall-clock second, clamped to the decoded audio. */
  private sampleIndex(seconds: number): number {
    const index = Math.round(seconds * this.sampleRate)
    if (!Number.isFinite(index) || index <= 0) {
      return 0
    }
    return Math.min(index, this.samples)
  }
}

// Chunk ids as big-endian u32, i.e. `'RIFF'`, `'WAVE'`, `'fmt '`, `'data'`.
const RIFF = 0x52494646
const WAVE = 0x57415645
const FMT = 0x666d7420
const DATA = 0x64617461

/**
 * Decode a RIFF/WAVE container of mono 16-bit PCM.
 *
 * Walks the chunks, so a `LIST` or `fact` chunk before `data` is fine; only
 * PCM format 1, mono, 16-bit is accepted, and anything else returns `null`
 * rather than throwing at the call site.
 */
export function decodePcmWav(bytes: ArrayBuffer): PcmTimeline | null {
  if (bytes.byteLength < 12) {
    return null
  }
  const view = new DataView(bytes)
  if (view.getUint32(0, false) !== RIFF || view.getUint32(8, false) !== WAVE) {
    return null
  }
  let format: number | null = null
  let channels = 0
  let bits = 0
  let sampleRate = 0
  let data: { offset: number; length: number } | null = null
  let cursor = 12
  while (cursor + 8 <= bytes.byteLength) {
    const id = view.getUint32(cursor, false)
    const size = view.getUint32(cursor + 4, true)
    const body = cursor + 8
    if (id === FMT && size >= 16 && body + 16 <= bytes.byteLength) {
      format = view.getUint16(body, true)
      channels = view.getUint16(body + 2, true)
      sampleRate = view.getUint32(body + 4, true)
      bits = view.getUint16(body + 14, true)
    } else if (id === DATA) {
      data = { offset: body, length: Math.min(size, bytes.byteLength - body) }
    }
    cursor = body + size + (size % 2)
  }
  if (format !== 1 || channels !== 1 || bits !== 16 || sampleRate <= 0 || data === null) {
    return null
  }
  const timeline = new PcmTimeline(sampleRate)
  timeline.appendS16(bytes.slice(data.offset, data.offset + data.length))
  return timeline
}
