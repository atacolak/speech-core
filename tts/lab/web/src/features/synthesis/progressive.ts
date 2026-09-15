import type { GenerateSegment } from '@/lib/api'

/** Playback queue surface, as the GENERATE pane reads it. */
export type QueueState = {
  index: number
  enqueued: number[]
  played: number[]
}

/**
 * Lowest index that is ready and not already queued.
 *
 * Strictly ordered: a lower segment that is still `generating` blocks the
 * queue instead of letting a later `generated` segment jump ahead.
 */
export function nextPlayable(segments: GenerateSegment[], enqueued: number[]): number | null {
  const done = new Set(enqueued)
  const ordered = [...segments].sort((a, b) => a.index - b.index)
  for (const item of ordered) {
    if (done.has(item.index)) {
      continue
    }
    return item.state === 'generated' ? item.index : null
  }
  return null
}

/**
 * Schedules decoded segment buffers on one audio-clock timeline.
 *
 * A later buffer starts at the earlier buffer's scheduled end, so playback is
 * gapless without a src swap, a timer, or an `<audio>` element.
 */
export class ProgressivePlayer {
  private readonly context: AudioContext
  private readonly onEnded?: (index: number) => void
  private readonly sources = new Set<AudioBufferSourceNode>()
  private readonly claimed = new Set<number>()
  private readonly enqueued: number[] = []
  private readonly played: number[] = []
  private scheduledEnd = 0
  private current = -1

  constructor(context?: AudioContext, onEnded?: (index: number) => void) {
    this.context = context ?? new AudioContext()
    this.onEnded = onEnded
  }

  get state(): QueueState {
    return { index: this.current, enqueued: [...this.enqueued], played: [...this.played] }
  }

  async enqueue(index: number, wavUrl: string): Promise<void> {
    if (this.claimed.has(index)) {
      return
    }
    this.claimed.add(index)
    try {
      const response = await fetch(wavUrl)
      if (!response.ok) {
        throw new Error(`segment audio ${response.status}`)
      }
      const buffer = await this.context.decodeAudioData(await response.arrayBuffer())
      const source = this.context.createBufferSource()
      source.buffer = buffer
      source.connect(this.context.destination)
      const when = Math.max(this.context.currentTime, this.scheduledEnd)
      source.start(when)
      this.scheduledEnd = when + buffer.duration
      this.current = index
      this.enqueued.push(index)
      this.sources.add(source)
      source.onended = () => {
        this.sources.delete(source)
        this.played.push(index)
        this.onEnded?.(index)
      }
    } catch (error) {
      this.claimed.delete(index)
      throw error
    }
  }

  /**
   * Stops the sources this player owns and resets playback.
   *
   * Separate from generation: cancelling a job is the caller's own request.
   */
  stop(): void {
    for (const source of this.sources) {
      source.onended = null
      source.stop()
    }
    this.sources.clear()
    this.claimed.clear()
    this.enqueued.length = 0
    this.played.length = 0
    this.scheduledEnd = 0
    this.current = -1
  }
}
