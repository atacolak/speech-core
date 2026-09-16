import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TakePlayer } from '@/components/take-player'
import { PcmTimeline } from '@/lib/pcm-timeline'

const SAMPLE_RATE = 4
const WAVE_HEIGHT = 128

function pcm(...samples: number[]): ArrayBuffer {
  const values = new Int16Array(samples)
  return values.buffer.slice(0)
}

/** `seconds` of silence at the fake sample rate, as one s16 chunk. */
function silence(seconds: number): ArrayBuffer {
  return pcm(...new Array(seconds * SAMPLE_RATE).fill(0))
}

type Started = { when: number; offset: number }

class FakeSource {
  buffer: AudioBuffer | null = null
  readonly started: Started[] = []
  stopped = false
  onended: (() => void) | null = null

  connect() {}
  disconnect() {}

  start(when: number, offset?: number) {
    this.started.push({ when, offset: offset ?? 0 })
  }

  stop() {
    this.stopped = true
  }

  /** The buffer ran out, as the browser reports it. */
  finish() {
    this.onended?.()
  }
}

class FakeAudioContext {
  static readonly contexts: FakeAudioContext[] = []
  static readonly sources: FakeSource[] = []

  readonly destination = {}
  currentTime = 0
  closed = false

  constructor() {
    FakeAudioContext.contexts.push(this)
  }

  createBuffer(_channels: number, length: number, sampleRate: number) {
    const channel = new Float32Array(length)
    return {
      length,
      sampleRate,
      duration: length / sampleRate,
      getChannelData: () => channel,
    } as unknown as AudioBuffer
  }

  createBufferSource() {
    const source = new FakeSource()
    FakeAudioContext.sources.push(source)
    return source as unknown as AudioBufferSourceNode
  }

  resume() {
    return Promise.resolve()
  }

  close() {
    this.closed = true
    return Promise.resolve()
  }
}

/** Records what the component paints, because jsdom has no real 2d context. */
class FakeGradient {
  readonly stops: Array<{ offset: number; color: string }> = []

  addColorStop(offset: number, color: string) {
    this.stops.push({ offset, color })
  }
}

type Fill = { x: number; y: number; width: number; height: number }

class FakeDrawing {
  fillStyle: unknown = ''
  readonly calls: string[] = []
  readonly gradients: FakeGradient[] = []
  readonly fills: Fill[] = []

  createLinearGradient() {
    this.calls.push('createLinearGradient')
    const gradient = new FakeGradient()
    this.gradients.push(gradient)
    return gradient
  }

  clearRect() {
    this.calls.push('clearRect')
  }

  beginPath() {
    this.calls.push('beginPath')
  }

  moveTo() {
    this.calls.push('moveTo')
  }

  lineTo() {
    this.calls.push('lineTo')
  }

  closePath() {
    this.calls.push('closePath')
  }

  fill() {
    this.calls.push('fill')
  }

  save() {
    this.calls.push('save')
  }

  clip() {
    this.calls.push('clip')
  }

  restore() {
    this.calls.push('restore')
  }

  fillRect(x: number, y: number, width: number, height: number) {
    this.calls.push('fillRect')
    this.fills.push({ x, y, width, height })
  }
}

let frames: FrameRequestCallback[] = []

/** jsdom has no 2d context; record what the component paints instead. */
function stubCanvas(): FakeDrawing {
  const drawing = new FakeDrawing()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    drawing as unknown as CanvasRenderingContext2D,
  )
  return drawing
}

const tick = () => {
  act(() => {
    frames.shift()?.(performance.now())
  })
}

function playButton() {
  return screen.getByRole('button', { name: 'Play' })
}

function waveSlider() {
  return screen.getByRole('slider', { name: 'Waveform' })
}

function waveValue() {
  return waveSlider().getAttribute('aria-valuenow')
}

function windowSlider() {
  return screen.getByRole('slider', { name: 'Window' })
}

function audio() {
  return FakeAudioContext.contexts[0]
}

function lastStart() {
  return FakeAudioContext.sources.at(-1)?.started.at(-1)
}

/** A take longer than the ten-second page, with a frame already in flight. */
function growingTake(seconds: number) {
  const timeline = new PcmTimeline(SAMPLE_RATE)
  timeline.appendS16(silence(seconds))
  return timeline
}

beforeEach(() => {
  FakeAudioContext.contexts.length = 0
  FakeAudioContext.sources.length = 0
  frames = []
  vi.stubGlobal('AudioContext', FakeAudioContext)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    frames.push(cb)
    return frames.length
  })
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('TakePlayer', () => {
  it('autoplays the first playable append once', () => {
    const timeline = new PcmTimeline(SAMPLE_RATE)
    const view = render(<TakePlayer timeline={timeline} autoplay live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(0)

    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    view.rerender(<TakePlayer timeline={timeline} autoplay live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(lastStart()).toEqual({ when: 0, offset: 0 })
    expect(screen.getByText('Take')).toBeInTheDocument()

    timeline.appendS16(pcm(4000, 5000, 6000, 7000))
    view.rerender(<TakePlayer timeline={timeline} autoplay live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it('advances the physical playhead from the audio clock', () => {
    const onPlayheadChange = vi.fn()
    const timeline = growingTake(11)
    render(
      <TakePlayer
        timeline={timeline}
        autoplay={false}
        live
        label="Take"
        onPlayheadChange={onPlayheadChange}
      />,
    )
    fireEvent.click(playButton())
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(waveValue()).toBe('0')

    audio().currentTime = 0.75
    tick()
    expect(waveValue()).toBe('0.75')

    audio().currentTime = 2.5
    tick()
    expect(waveValue()).toBe('2.5')

    // The position comes from the clock, never from another source.
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(onPlayheadChange).toHaveBeenLastCalledWith(2.5)
  })

  it('keeps playhead and window stable when PCM appends', () => {
    const timeline = growingTake(11)
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())
    audio().currentTime = 3
    tick()
    expect(waveValue()).toBe('3')
    expect(windowSlider()).toHaveValue('0')

    timeline.appendS16(silence(4))
    view.rerender(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(waveValue()).toBe('3')
    expect(windowSlider()).toHaveValue('0')
    expect(FakeAudioContext.sources).toHaveLength(1)

    tick()
    expect(waveValue()).toBe('3')
    expect(windowSlider()).toHaveValue('0')
  })

  it('scrolls the window without seeking', () => {
    const timeline = growingTake(13)
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())
    expect(lastStart()).toEqual({ when: 0, offset: 0 })

    fireEvent.change(windowSlider(), { target: { value: '3' } })

    expect(windowSlider()).toHaveValue('3')
    expect(waveValue()).toBe('0')
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(lastStart()).toEqual({ when: 0, offset: 0 })
  })

  it('restarts from zero only after an explicit Play at end', () => {
    const timeline = growingTake(1)
    render(<TakePlayer timeline={timeline} autoplay={false} live={false} label="Take" />)
    fireEvent.click(playButton())

    act(() => {
      FakeAudioContext.sources[0].finish()
    })

    expect(waveValue()).toBe('1')
    expect(playButton()).toBeInTheDocument()
    // Reaching the end naturally parks; only Play restarts.
    expect(FakeAudioContext.sources).toHaveLength(1)

    fireEvent.click(playButton())
    expect(FakeAudioContext.sources).toHaveLength(2)
    expect(lastStart()).toEqual({ when: 0, offset: 0 })
    expect(waveValue()).toBe('0')
  })

  it('renders a 128 px soft wave and borderless transport', () => {
    const drawing = stubCanvas()
    const timeline = growingTake(13)
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)

    const wave = waveSlider()
    expect(wave).toHaveAttribute('height', '128')
    expect(wave.className).toContain('h-32')
    expect(wave.className).not.toContain('border')

    for (const name of ['Play', 'Back 5 seconds', 'Forward 5 seconds']) {
      expect(screen.getByRole('button', { name }).className).not.toContain('border')
    }

    // One filled mirrored envelope, softened by a vertical gradient.
    expect(drawing.calls).toContain('closePath')
    expect(drawing.calls).toContain('fill')
    expect(drawing.calls).toContain('createLinearGradient')
    expect(drawing.gradients.some((gradient) => gradient.stops.length >= 3)).toBe(true)
    // A thin playhead marker over the envelope.
    expect(
      drawing.fills.some((fill) => fill.width <= 2 && fill.height === WAVE_HEIGHT),
    ).toBe(true)

    // Wave, then the optional Window scroll, then the transport.
    const order = [wave, windowSlider(), playButton()]
    expect(order[0].compareDocumentPosition(order[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(order[1].compareDocumentPosition(order[2]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('uses the full width below ten seconds and pages over ten seconds', () => {
    stubCanvas()
    const peaks = vi.spyOn(PcmTimeline.prototype, 'peaksRange')
    const timeline = growingTake(4)
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(screen.queryByRole('slider', { name: 'Window' })).toBeNull()
    expect(peaks.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, 4])

    timeline.appendS16(silence(7))
    view.rerender(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(windowSlider()).toHaveAttribute('max', '1')
    expect(peaks.mock.calls.at(-1)?.slice(0, 2)).toEqual([0, 10])
  })

  it('renders no Seek range and no current / total row', () => {
    const timeline = growingTake(11)
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(screen.queryByRole('slider', { name: 'Seek' })).toBeNull()
    expect(screen.queryByText(/\d+\.\d\ds\s*\/\s*\d/)).toBeNull()
    expect(waveValue()).toBe('0')
  })

  it('maps wave pointer seeks through the visible window', () => {
    const timeline = growingTake(18)
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())
    fireEvent.change(windowSlider(), { target: { value: '8' } })

    const wave = waveSlider()
    wave.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: WAVE_HEIGHT }) as DOMRect
    fireEvent.pointerDown(wave, { clientX: 100 })

    // Half of the eight-second window is five seconds past its start.
    expect(waveValue()).toBe('13')
    expect(FakeAudioContext.sources).toHaveLength(2)
    expect(lastStart()).toEqual({ when: 0, offset: 13 })
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it('pauses and skips within already-produced audio', () => {
    const timeline = growingTake(11)
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(waveValue()).toBe('0')

    fireEvent.click(screen.getByRole('button', { name: 'Forward 5 seconds' }))
    expect(waveValue()).toBe('5')
    expect(FakeAudioContext.sources).toHaveLength(0)

    fireEvent.click(playButton())
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(lastStart()).toEqual({ when: 0, offset: 5 })

    fireEvent.click(screen.getByRole('button', { name: 'Back 5 seconds' }))
    expect(waveValue()).toBe('0')
    expect(FakeAudioContext.sources).toHaveLength(2)
    expect(lastStart()).toEqual({ when: 0, offset: 0 })

    fireEvent.click(screen.getByRole('button', { name: 'Forward 5 seconds' }))
    expect(lastStart()).toEqual({ when: 0, offset: 5 })

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(playButton()).toBeInTheDocument()
    expect(FakeAudioContext.sources[2].stopped).toBe(true)
  })

  it('keeps a live take rolling when the buffer runs out mid-stream', () => {
    const timeline = growingTake(1)
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())
    expect(FakeAudioContext.sources).toHaveLength(1)

    act(() => {
      FakeAudioContext.sources[0].finish()
    })
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
    expect(waveValue()).toBe('1')
    expect(FakeAudioContext.sources).toHaveLength(1)

    timeline.appendS16(silence(1))
    view.rerender(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(2)
    expect(lastStart()).toEqual({ when: 0, offset: 1 })
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it('finishes PCM that arrived before the source buffer ended', () => {
    const timeline = growingTake(1)
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live={false} label="Take" />)
    fireEvent.click(playButton())

    timeline.appendS16(silence(1))
    view.rerender(<TakePlayer timeline={timeline} autoplay={false} live={false} label="Take" />)
    act(() => {
      FakeAudioContext.sources[0].finish()
    })

    expect(FakeAudioContext.sources).toHaveLength(2)
    expect(lastStart()).toEqual({ when: 0, offset: 1 })
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()

    act(() => {
      FakeAudioContext.sources[1].finish()
    })
    expect(playButton()).toBeInTheDocument()
    expect(waveValue()).toBe('2')
    expect(FakeAudioContext.sources).toHaveLength(2)
  })

  it('pauses on the position it parked, not the stale clock', () => {
    const timeline = growingTake(1)
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())
    act(() => {
      FakeAudioContext.sources[0].finish()
    })
    expect(waveValue()).toBe('1')

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    expect(waveValue()).toBe('1')
    expect(playButton()).toBeInTheDocument()
  })

  it('sends no Stop request and keeps appending after transport actions', () => {
    const requests = vi.fn()
    vi.stubGlobal('fetch', requests)
    const timeline = growingTake(1)
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    fireEvent.click(screen.getByRole('button', { name: 'Back 5 seconds' }))
    fireEvent.click(screen.getByRole('button', { name: 'Forward 5 seconds' }))
    expect(requests).not.toHaveBeenCalled()

    timeline.appendS16(silence(2))
    view.rerender(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(waveSlider().getAttribute('aria-valuemax')).toBe('3')
    expect(playButton()).toBeInTheDocument()
  })

  it('renders no audio element and stops its source on unmount', () => {
    const timeline = growingTake(1)
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(document.querySelector('audio')).toBeNull()

    fireEvent.click(playButton())
    const source = FakeAudioContext.sources[0]
    view.unmount()
    expect(source.stopped).toBe(true)
    expect(FakeAudioContext.contexts[0].closed).toBe(true)
  })
})
