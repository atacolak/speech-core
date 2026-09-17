/** The visible wave window. Ten seconds is the operator's physical page size. */
export const MAX_VISIBLE_S = 10

/** Unplayed wave kept to the right of the playhead, so the page never runs dry. */
export const LOOKAHEAD_S = 3

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
 * Keep the audible playhead inside the window with a 3s unplayed tail ahead of
 * it: the viewport moves forward once the playhead crosses `visibleS - 3`, and
 * back when it falls behind the left edge. A growing take re-anchors the window
 * because the playhead crossed the lookahead, not because the duration changed.
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
  const keep = Math.max(window.visibleS - LOOKAHEAD_S, 0)
  if (playhead > window.startS + keep) {
    return waveWindow(duration, playhead - keep)
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
