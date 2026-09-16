/** The visible wave window. Ten seconds is the operator's physical page size. */
export const MAX_VISIBLE_S = 10

export type WaveWindow = {
  startS: number
  visibleS: number
  maxStartS: number
  scrollable: boolean
}

/** Seconds that are not finite and non-negative are not a duration or a position. */
function clampedSeconds(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0
}

/**
 * The window a take of `durationS` shows from `requestedStartS`.
 *
 * Below ten seconds the wave uses the full width; above it the visible duration
 * stays ten seconds and only the start moves. A shorter take has nothing to
 * scroll, so the request collapses to its only window.
 */
export function waveWindow(durationS: number, requestedStartS: number): WaveWindow {
  const duration = clampedSeconds(durationS)
  const visibleS = Math.min(duration, MAX_VISIBLE_S)
  const maxStartS = duration - visibleS
  const startS = Number.isFinite(requestedStartS)
    ? Math.min(Math.max(requestedStartS, 0), maxStartS)
    : 0
  return { startS, visibleS, maxStartS, scrollable: maxStartS > 0 }
}

/**
 * Keep the audible playhead inside the window, moving the viewport only when it
 * leaves: forward past the right edge, or back behind the left one. A growing
 * take never re-anchors the window, because the playhead has not moved.
 */
export function followPlayhead(
  window: WaveWindow,
  durationS: number,
  playheadS: number,
): WaveWindow {
  const duration = clampedSeconds(durationS)
  const playhead = Math.min(clampedSeconds(playheadS), duration)
  if (playhead < window.startS) {
    return waveWindow(duration, playhead)
  }
  if (playhead > window.startS + window.visibleS) {
    return waveWindow(duration, playhead - window.visibleS)
  }
  return waveWindow(duration, window.startS)
}

/** Where `seconds` sits inside the visible window as 0..1; `null` when off-window. */
export function windowFraction(window: WaveWindow, seconds: number): number | null {
  if (!(window.visibleS > 0) || !Number.isFinite(seconds)) {
    return null
  }
  const offset = seconds - window.startS
  if (offset < 0 || offset > window.visibleS) {
    return null
  }
  return offset / window.visibleS
}
