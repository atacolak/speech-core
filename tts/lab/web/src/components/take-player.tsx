import { useCallback, useEffect, useRef, useState } from 'react'
import { Pause, Play, SkipBack, SkipForward } from 'lucide-react'
import type {
  PointerEvent as ReactPointerEvent,
  ReactElement,
  WheelEvent as ReactWheelEvent,
} from 'react'
import type { PcmTimeline } from '@/lib/pcm-timeline'
import { followPlayhead, waveWindow, windowFraction } from '@/lib/wave-window'
import type { WaveWindow } from '@/lib/wave-window'

const SKIP_S = 5
const EPSILON_S = 1e-6
const WAVE_WIDTH = 1080
const WAVE_HEIGHT = 128
const SILENT_STOPS = ['rgba(113,113,122,0.25)', 'rgba(161,161,170,0.6)', 'rgba(113,113,122,0.25)'] as const
const PLAYED_STOPS = ['rgba(56,189,248,0.35)', 'rgba(56,189,248,0.95)', 'rgba(56,189,248,0.35)'] as const

/** Vertical softness for one envelope fill: quiet at both edges, solid at the axis. */
function envelopeGradient(
  drawing: CanvasRenderingContext2D,
  height: number,
  stops: readonly [string, string, string],
): CanvasGradient {
  const gradient = drawing.createLinearGradient(0, 0, 0, height)
  gradient.addColorStop(0, stops[0])
  gradient.addColorStop(0.5, stops[1])
  gradient.addColorStop(1, stops[2])
  return gradient
}

/** One mirrored filled envelope of the visible window, with the played part lit. */
function paintWave(
  drawing: CanvasRenderingContext2D,
  canvas: HTMLCanvasElement,
  timeline: PcmTimeline,
  visible: WaveWindow,
  playhead: number,
): void {
  const { width, height } = canvas
  const axis = height / 2
  const peaks = timeline.peaksRange(
    visible.startS,
    visible.startS + visible.visibleS,
    Math.max(1, Math.floor(width)),
  )
  const slot = width / Math.max(peaks.length, 1)
  drawing.clearRect(0, 0, width, height)
  drawing.beginPath()
  drawing.moveTo(0, axis)
  peaks.forEach((peak, index) => {
    drawing.lineTo(index * slot, axis - peak.max * axis * 0.92)
  })
  for (let index = peaks.length - 1; index >= 0; index -= 1) {
    drawing.lineTo(index * slot, axis - peaks[index].min * axis * 0.92)
  }
  drawing.closePath()
  drawing.fillStyle = envelopeGradient(drawing, height, SILENT_STOPS)
  drawing.fill()

  const fraction = windowFraction(visible, playhead)
  const played = Math.min(Math.max(fraction ?? (playhead < visible.startS ? 0 : 1), 0), 1)
  drawing.save()
  drawing.clip()
  drawing.fillStyle = envelopeGradient(drawing, height, PLAYED_STOPS)
  drawing.fillRect(0, 0, played * width, height)
  drawing.restore()

}

/**
 * A physical window over one growing take: the playhead is read from the audio
 * clock every animation frame, the wave shows at most ten seconds, and a
 * separate scrollbar moves the viewport without ever moving the playhead.
 *
 * The buffer is read from `timeline` when a source starts, so an append only
 * lengthens what the next start would read; it never re-instantiates a source
 * URL, which is what keeps playback across growth.
 */
export function TakePlayer({
  timeline,
  autoplay,
  live,
  label,
  onPlayheadChange,
}: {
  timeline: PcmTimeline | null
  autoplay: boolean
  live: boolean
  label?: string
  onPlayheadChange?: (seconds: number) => void
}): ReactElement | null {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const sourceRef = useRef<AudioBufferSourceNode | null>(null)
  const drawingRef = useRef<CanvasRenderingContext2D | null>(null)
  const drawingReadRef = useRef(false)
  const frameRef = useRef<number | null>(null)
  const timelineRef = useRef<PcmTimeline | null>(timeline)
  const liveRef = useRef(live)
  const onPlayheadChangeRef = useRef(onPlayheadChange)
  const draggingRef = useRef<{ x: number; startS: number; moved: boolean } | null>(null)
  const autoplayedRef = useRef(false)
  const playingRef = useRef(false)
  const playheadRef = useRef(0)
  const startedFromRef = useRef(0)
  const startedAtContextRef = useRef(0)
  const windowStartRef = useRef(0)
  const reportedRef = useRef(Number.NaN)
  const [playing, setPlaying] = useState(false)
  const [windowStartS, setWindowStartS] = useState(0)

  timelineRef.current = timeline
  liveRef.current = live
  onPlayheadChangeRef.current = onPlayheadChange
  const length = timeline?.length ?? 0
  const durationS = timeline?.durationS ?? 0
  const visible = waveWindow(durationS, windowStartS)
  windowStartRef.current = visible.startS

  function ensureContext(): AudioContext {
    if (contextRef.current === null) {
      contextRef.current = new window.AudioContext()
    }
    return contextRef.current
  }

  function stopFrames(): void {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current)
      frameRef.current = null
    }
  }

  function retireSource(): void {
    const source = sourceRef.current
    sourceRef.current = null
    if (source !== null) {
      source.onended = null
      source.stop()
    }
  }

  /** Move the playhead to `seconds`, dragging the visible window with it. */
  function advanceTo(seconds: number): void {
    const current = timelineRef.current
    const duration = current?.durationS ?? 0
    const limit = current === null ? Math.max(seconds, 0) : duration
    playheadRef.current = Math.min(Math.max(Number.isFinite(seconds) ? seconds : 0, 0), limit)
    const shown = waveWindow(duration, windowStartRef.current)
    if (!shown.scrollable) {
      return
    }
    const followed = followPlayhead(shown, duration, playheadRef.current)
    if (followed.startS !== shown.startS) {
      windowStartRef.current = followed.startS
      setWindowStartS(followed.startS)
    }
  }

  /**
   * Write the audible position to the canvas and the consumer without a React
   * render: only the transport's play/pause flag is component state.
   */
  function paint(): void {
    const canvas = canvasRef.current
    const current = timelineRef.current
    if (canvas === null || current === null) {
      return
    }
    const duration = current.durationS
    const playhead = Math.min(Math.max(playheadRef.current, 0), duration)
    canvas.setAttribute('aria-valuemin', '0')
    canvas.setAttribute('aria-valuemax', String(duration))
    canvas.setAttribute('aria-valuenow', String(playhead))
    if (playhead !== reportedRef.current) {
      reportedRef.current = playhead
      onPlayheadChangeRef.current?.(playhead)
    }
    if (!drawingReadRef.current) {
      drawingReadRef.current = true
      drawingRef.current = canvas.getContext('2d')
    }
    if (drawingRef.current === null) {
      return
    }
    paintWave(drawingRef.current, canvas, current, waveWindow(duration, windowStartRef.current), playhead)
  }

  function onFrame(): void {
    frameRef.current = null
    const audio = contextRef.current
    const current = timelineRef.current
    if (!playingRef.current || audio === null || current === null) {
      return
    }
    advanceTo(startedFromRef.current + (audio.currentTime - startedAtContextRef.current))
    paint()
    scheduleFrame()
  }

  function scheduleFrame(): void {
    if (frameRef.current !== null || !playingRef.current) {
      return
    }
    frameRef.current = requestAnimationFrame(onFrame)
  }

  const startFrom = useCallback((offset: number) => {
    const current = timelineRef.current
    if (current === null || current.length === 0) {
      return
    }
    const audio = ensureContext()
    void audio.resume()
    const samples = current.toFloat32()
    const buffer = audio.createBuffer(1, samples.length, current.sampleRate)
    buffer.getChannelData(0).set(samples)
    const clamped = Math.min(Math.max(offset, 0), current.durationS)
    const soundedTo = current.durationS
    const next = audio.createBufferSource()
    next.buffer = buffer
    next.connect(audio.destination)
    next.onended = () => {
      if (sourceRef.current !== next) {
        return
      }
      sourceRef.current = null
      if (!playingRef.current) {
        return
      }
      const latest = timelineRef.current
      const from = Math.min(soundedTo, latest?.durationS ?? soundedTo)
      playheadRef.current = from
      stopFrames()
      if (latest !== null && latest.durationS > soundedTo + EPSILON_S) {
        // PCM that landed while this buffer played: finish the take it started.
        startFrom(soundedTo)
        return
      }
      if (liveRef.current) {
        // The stream is still running: stay armed and roll on with the next
        // chunk when it arrives.
        paint()
        return
      }
      playingRef.current = false
      setPlaying(false)
      paint()
    }
    retireSource()
    sourceRef.current = next
    playingRef.current = true
    setPlaying(true)
    startedFromRef.current = clamped
    startedAtContextRef.current = audio.currentTime
    next.start(audio.currentTime, clamped)
    advanceTo(clamped)
    paint()
    scheduleFrame()
  }, [])

  /** Park the playhead on the clock, then keep the visible page where it is. */
  function pause(): void {
    const audio = contextRef.current
    const current = timelineRef.current
    // Only a running source has a clock position; a parked transport keeps the
    // position it parked on.
    if (playingRef.current && sourceRef.current !== null && audio !== null && current !== null) {
      playheadRef.current = Math.min(
        Math.max(startedFromRef.current + (audio.currentTime - startedAtContextRef.current), 0),
        current.durationS,
      )
    }
    playingRef.current = false
    setPlaying(false)
    stopFrames()
    retireSource()
    paint()
  }

  function seek(next: number): void {
    const current = timelineRef.current
    if (current === null) {
      return
    }
    const clamped = Math.min(Math.max(next, 0), current.durationS)
    if (playingRef.current) {
      startFrom(clamped)
      return
    }
    advanceTo(clamped)
    paint()
  }

  function seekFromPointer(event: ReactPointerEvent<HTMLCanvasElement>): void {
    const rect = event.currentTarget.getBoundingClientRect()
    const current = timelineRef.current
    if (rect.width <= 0 || current === null) {
      return
    }
    const shown = waveWindow(current.durationS, windowStartRef.current)
    const fraction = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1)
    seek(shown.startS + fraction * shown.visibleS)
  }
  function panFromPointer(event: ReactPointerEvent<HTMLCanvasElement>): void {
    const drag = draggingRef.current
    const rect = event.currentTarget.getBoundingClientRect()
    const current = timelineRef.current
    if (drag === null || rect.width <= 0 || current === null) {
      return
    }
    const shown = waveWindow(current.durationS, drag.startS)
    if (!shown.scrollable) {
      return
    }
    const dx = event.clientX - drag.x
    if (Math.abs(dx) < 4) {
      return
    }
    drag.moved = true
    const next = waveWindow(current.durationS, drag.startS - (dx / rect.width) * shown.visibleS)
    windowStartRef.current = next.startS
    setWindowStartS(next.startS)
  }
  function panFromWheel(event: ReactWheelEvent<HTMLCanvasElement>): void {
    const rect = event.currentTarget.getBoundingClientRect()
    const current = timelineRef.current
    const shown = waveWindow(current?.durationS ?? 0, windowStartRef.current)
    if (rect.width <= 0 || current === null || !shown.scrollable) {
      return
    }
    const delta = event.deltaX !== 0 ? event.deltaX : event.deltaY
    if (delta === 0) {
      return
    }
    event.preventDefault()
    const next = waveWindow(current.durationS, shown.startS + (delta / rect.width) * shown.visibleS)
    windowStartRef.current = next.startS
    setWindowStartS(next.startS)
  }

  useEffect(() => {
    if (!autoplay || autoplayedRef.current || length === 0) {
      return
    }
    autoplayedRef.current = true
    startFrom(0)
  }, [autoplay, length, startFrom])

  // A live stream that outran the buffer keeps its place and rolls on with the
  // next chunk; nothing starts while a source is still active.
  useEffect(() => {
    if (!live || sourceRef.current !== null || !playingRef.current) {
      return
    }
    const current = timelineRef.current
    if (current === null || current.durationS <= playheadRef.current + EPSILON_S) {
      return
    }
    startFrom(playheadRef.current)
  }, [live, length, startFrom])

  // An append lengthens the take behind the playhead; it never moves it.
  useEffect(() => {
    const current = timelineRef.current
    if (current !== null) {
      playheadRef.current = Math.min(playheadRef.current, current.durationS)
    }
    paint()
  }, [length, windowStartS])

  useEffect(() => () => {
    playingRef.current = false
    stopFrames()
    retireSource()
    const audio = contextRef.current
    contextRef.current = null
    if (audio !== null) {
      void audio.close()
    }
  }, [])

  if (timeline === null || timeline.length === 0) {
    return null
  }

  return (
    <div className="flex flex-col gap-2">
      {label ? (
        <p className="text-[11px] font-medium uppercase tracking-wide text-zinc-400">{label}</p>
      ) : null}
      <canvas
        ref={canvasRef}
        aria-label="Waveform"
        className="h-32 w-full cursor-pointer rounded-md bg-zinc-950"
        height={WAVE_HEIGHT}
        onPointerDown={(event) => {
          draggingRef.current = { x: event.clientX, startS: windowStartRef.current, moved: false }
        }}
        onPointerMove={panFromPointer}
        onPointerUp={(event) => {
          const drag = draggingRef.current
          draggingRef.current = null
          if (drag !== null && !drag.moved) {
            seekFromPointer(event)
          }
        }}
        onPointerCancel={() => {
          draggingRef.current = null
        }}
        onWheel={panFromWheel}
        role="slider"
        width={WAVE_WIDTH}
      />
      {visible.scrollable ? (
        <input
          aria-label="Window"
          className="h-0.5 w-full appearance-none bg-zinc-800/70 [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-zinc-500 [&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-4 [&::-moz-range-thumb]:appearance-none [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-zinc-500"
          max={visible.maxStartS}
          min={0}
          onChange={(event) => {
            const requested = Math.min(Math.max(Number(event.target.value), 0), visible.maxStartS)
            windowStartRef.current = requested
            setWindowStartS(requested)
          }}
          step="any"
          type="range"
          value={visible.startS}
        />
      ) : null}
      <div className="flex items-center justify-center gap-4 text-zinc-400">
        <button
          aria-label={`Back ${SKIP_S} seconds`}
          className="rounded p-1 hover:text-zinc-100"
          onClick={() => seek(playheadRef.current - SKIP_S)}
          type="button"
        >
          <SkipBack aria-hidden="true" className="h-5 w-5" />
        </button>
        <button
          aria-label={playing ? 'Pause' : 'Play'}
          className="rounded p-1 text-zinc-100 hover:text-white"
          onClick={() => {
            if (playingRef.current) {
              pause()
              return
            }
            startFrom(playheadRef.current >= durationS - EPSILON_S ? 0 : playheadRef.current)
          }}
          type="button"
        >
          {playing ? (
            <Pause aria-hidden="true" className="h-6 w-6" />
          ) : (
            <Play aria-hidden="true" className="h-6 w-6" />
          )}
        </button>
        <button
          aria-label={`Forward ${SKIP_S} seconds`}
          className="rounded p-1 hover:text-zinc-100"
          onClick={() => seek(playheadRef.current + SKIP_S)}
          type="button"
        >
          <SkipForward aria-hidden="true" className="h-5 w-5" />
        </button>
      </div>
    </div>
  )
}
