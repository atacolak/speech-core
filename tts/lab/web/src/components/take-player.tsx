import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactElement } from 'react'
import { formatSeconds } from '@/lib/format'
import type { PcmTimeline } from '@/lib/pcm-timeline'

const SKIP_S = 5
const EPSILON_S = 1e-6

/**
 * Compact growing-take transport: one canvas waveform, play/pause, ±5 s, seek.
 *
 * The buffer is read from `timeline` when a source starts, so an append only
 * lengthens what the next start would read; it never re-instantiates a source
 * URL, which is what keeps `playing` and `offsetS` across growth.
 */
export function TakePlayer({
  timeline,
  autoplay,
  live,
  label,
}: {
  timeline: PcmTimeline | null
  autoplay: boolean
  live: boolean
  label?: string
}): ReactElement | null {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const sourceRef = useRef<AudioBufferSourceNode | null>(null)
  const timelineRef = useRef<PcmTimeline | null>(timeline)
  const liveRef = useRef(live)
  const draggingRef = useRef(false)
  const autoplayedRef = useRef(false)
  const playingRef = useRef(false)
  const offsetRef = useRef(0)
  const [playing, setPlaying] = useState(false)
  const [offsetS, setOffsetS] = useState(0)

  timelineRef.current = timeline
  liveRef.current = live
  const length = timeline?.length ?? 0
  const durationS = timeline?.durationS ?? 0

  function ensureContext(): AudioContext {
    if (contextRef.current === null) {
      contextRef.current = new window.AudioContext()
    }
    return contextRef.current
  }

  function retireSource(): void {
    const source = sourceRef.current
    sourceRef.current = null
    if (source !== null) {
      source.onended = null
      source.stop()
    }
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
    const playedTo = current.durationS
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
      offsetRef.current = playedTo
      setOffsetS(playedTo)
      if (latest !== null && latest.durationS > playedTo + EPSILON_S) {
        startFrom(playedTo)
        return
      }
      if (!liveRef.current) {
        playingRef.current = false
        setPlaying(false)
      }
    }
    retireSource()
    sourceRef.current = next
    playingRef.current = true
    setPlaying(true)
    offsetRef.current = clamped
    setOffsetS(clamped)
    next.start(audio.currentTime, clamped)
  }, [])

  function pause(): void {
    playingRef.current = false
    setPlaying(false)
    retireSource()
  }

  function seek(next: number): void {
    const current = timelineRef.current
    if (current === null) {
      return
    }
    const clamped = Math.min(Math.max(next, 0), current.durationS)
    offsetRef.current = clamped
    setOffsetS(clamped)
    if (playingRef.current) {
      startFrom(clamped)
    }
  }

  function seekFromPointer(event: ReactPointerEvent<HTMLCanvasElement>): void {
    const rect = event.currentTarget.getBoundingClientRect()
    const current = timelineRef.current
    if (rect.width <= 0 || current === null) {
      return
    }
    seek(((event.clientX - rect.left) / rect.width) * current.durationS)
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
    if (current === null || current.durationS <= offsetRef.current + EPSILON_S) {
      return
    }
    startFrom(offsetRef.current)
  }, [live, length, startFrom])

  useEffect(() => {
    const canvas = canvasRef.current
    const current = timelineRef.current
    const drawing = canvas?.getContext('2d') ?? null
    if (canvas === null || drawing === null || current === null) {
      return
    }
    const { width, height } = canvas
    const peaks = current.peaks(Math.max(1, Math.floor(width / 2)))
    const slot = width / Math.max(peaks.length, 1)
    drawing.clearRect(0, 0, width, height)
    drawing.fillStyle = '#71717a'
    peaks.forEach((peak, index) => {
      const top = height / 2 - peak.max * (height / 2)
      const bottom = height / 2 - peak.min * (height / 2)
      drawing.fillRect(index * slot, top, Math.max(slot - 1, 1), Math.max(bottom - top, 1))
    })
    if (current.durationS > 0) {
      drawing.fillStyle = '#38bdf8'
      drawing.fillRect((offsetS / current.durationS) * width, 0, 2, height)
    }
  }, [length, offsetS])

  useEffect(() => () => {
    playingRef.current = false
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
        className="h-16 w-full cursor-pointer rounded-md border border-zinc-700 bg-zinc-950"
        height={64}
        onPointerDown={(event) => {
          draggingRef.current = true
          seekFromPointer(event)
        }}
        onPointerLeave={() => {
          draggingRef.current = false
        }}
        onPointerMove={(event) => {
          if (draggingRef.current) {
            seekFromPointer(event)
          }
        }}
        onPointerUp={() => {
          draggingRef.current = false
        }}
        width={360}
      />
      <div className="flex items-center gap-2 text-xs text-zinc-300">
        <button
          type="button"
          className="rounded-md border border-zinc-500 px-3 py-1"
          onClick={() => {
            if (playing) {
              pause()
            } else {
              startFrom(offsetRef.current)
            }
          }}
        >
          {playing ? 'Pause' : 'Play'}
        </button>
        <button
          type="button"
          className="rounded-md border border-zinc-500 px-2 py-1"
          onClick={() => seek(offsetRef.current - SKIP_S)}
        >
          Back {SKIP_S} seconds
        </button>
        <button
          type="button"
          className="rounded-md border border-zinc-500 px-2 py-1"
          onClick={() => seek(offsetRef.current + SKIP_S)}
        >
          Forward {SKIP_S} seconds
        </button>
        <input
          aria-label="Seek"
          className="min-w-0 flex-1 accent-zinc-200"
          max={durationS}
          min={0}
          onChange={(event) => seek(Number(event.target.value))}
          step="any"
          type="range"
          value={offsetS}
        />
        <span className="tabular-nums text-zinc-400">
          {formatSeconds(offsetS)} / {formatSeconds(durationS)}
        </span>
      </div>
    </div>
  )
}
