import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TakePlayer } from '@/components/take-player'
import { PcmTimeline } from '@/lib/pcm-timeline'

function pcm(...samples: number[]): ArrayBuffer {
  const values = new Int16Array(samples)
  return values.buffer.slice(0)
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

function playButton() {
  return screen.getByRole('button', { name: 'Play' })
}

function seekSlider() {
  return screen.getByRole('slider', { name: 'Seek' })
}

beforeEach(() => {
  FakeAudioContext.contexts.length = 0
  FakeAudioContext.sources.length = 0
  vi.stubGlobal('AudioContext', FakeAudioContext)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('TakePlayer', () => {
  it('autoplays the first playable append once', () => {
    const timeline = new PcmTimeline(4)
    const view = render(<TakePlayer timeline={timeline} autoplay live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(0)

    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    view.rerender(<TakePlayer timeline={timeline} autoplay live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(FakeAudioContext.sources[0].started.at(-1)).toEqual({ when: 0, offset: 0 })
    expect(screen.getByText('Take')).toBeInTheDocument()

    timeline.appendS16(pcm(4000, 5000, 6000, 7000))
    view.rerender(<TakePlayer timeline={timeline} autoplay live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it('preserves playing and seek offset when the timeline grows', async () => {
    const timeline = new PcmTimeline(4)
    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(screen.getByRole('button', { name: 'Play' }))
    fireEvent.change(screen.getByRole('slider', { name: 'Seek' }), { target: { value: '0.5' } })
    expect(FakeAudioContext.sources).toHaveLength(2)
    timeline.appendS16(pcm(4000, 5000, 6000, 7000))
    view.rerender(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
    expect(screen.getByRole('slider', { name: 'Seek' })).toHaveValue('0.5')
    expect(FakeAudioContext.sources).toHaveLength(2)
  })

  it('pauses and seeks within already-produced audio', () => {
    const timeline = new PcmTimeline(4)
    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(seekSlider()).toHaveValue('0')

    fireEvent.click(screen.getByRole('button', { name: 'Forward 5 seconds' }))
    expect(seekSlider()).toHaveValue('1')
    expect(FakeAudioContext.sources).toHaveLength(0)

    fireEvent.click(playButton())
    expect(FakeAudioContext.sources).toHaveLength(1)
    expect(FakeAudioContext.sources[0].started.at(-1)).toEqual({ when: 0, offset: 1 })

    fireEvent.click(screen.getByRole('button', { name: 'Back 5 seconds' }))
    expect(seekSlider()).toHaveValue('0')
    expect(FakeAudioContext.sources).toHaveLength(2)
    expect(FakeAudioContext.sources[1].started.at(-1)).toEqual({ when: 0, offset: 0 })

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(playButton()).toBeInTheDocument()
    expect(FakeAudioContext.sources[1].stopped).toBe(true)
  })

  it('seeks from the waveform and keeps playing', () => {
    const timeline = new PcmTimeline(4)
    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())

    const waveform = screen.getByLabelText('Waveform')
    waveform.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 64 }) as unknown as DOMRect
    fireEvent.pointerDown(waveform, { clientX: 50 })
    expect(seekSlider()).toHaveValue('0.25')
    fireEvent.pointerMove(waveform, { clientX: 150 })
    expect(seekSlider()).toHaveValue('0.75')
    expect(FakeAudioContext.sources).toHaveLength(3)
  })

  it('keeps a live take rolling when the buffer runs out mid-stream', () => {
    const timeline = new PcmTimeline(4)
    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    fireEvent.click(playButton())
    expect(FakeAudioContext.sources).toHaveLength(1)

    act(() => FakeAudioContext.sources[0].finish())
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
    expect(FakeAudioContext.sources).toHaveLength(1)

    timeline.appendS16(pcm(4000, 5000, 6000, 7000))
    view.rerender(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(FakeAudioContext.sources).toHaveLength(2)
    expect(FakeAudioContext.sources[1].started.at(-1)).toEqual({ when: 0, offset: 1 })
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it('stops at the end of the produced audio when not live', () => {
    const timeline = new PcmTimeline(4)
    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    render(<TakePlayer timeline={timeline} autoplay={false} live={false} label="Take" />)
    expect(FakeAudioContext.contexts).toHaveLength(0)

    fireEvent.click(playButton())

    act(() => FakeAudioContext.sources[0].finish())
    expect(playButton()).toBeInTheDocument()
    expect(seekSlider()).toHaveValue('1')
  })

  it('renders no audio element and stops its source on unmount', () => {
    const timeline = new PcmTimeline(4)
    timeline.appendS16(pcm(0, 1000, 2000, 3000))
    const view = render(<TakePlayer timeline={timeline} autoplay={false} live label="Take" />)
    expect(document.querySelector('audio')).toBeNull()

    fireEvent.click(playButton())
    const source = FakeAudioContext.sources[0]
    view.unmount()
    expect(source.stopped).toBe(true)
    expect(FakeAudioContext.contexts[0].closed).toBe(true)
  })
})
