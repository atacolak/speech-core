import type { RunItem } from '@/lib/api'

export type TakeTranscript = { text: string; truncated: boolean }

/**
 * The text a take card may show for a settled run.
 *
 * `produced_text` is the exact prefix that became audio, so a run whose unborn
 * segments never started shows that prefix plus an ellipsis and nothing else.
 * Truncation follows the segment counts, never `stopped`: a client disconnect
 * leaves `stopped` false while segments are still missing, and a Stop that
 * arrives after the final segment leaves it true with nothing missing.
 *
 * Old one-shot runs carry no produced prefix; they fall back to the requested
 * Say text with no ellipsis, because nothing is known to be missing.
 */
export function takeTranscript(run: RunItem): TakeTranscript {
  const snapshot = run.request_snapshot
  const produced = snapshot?.produced_text
  if (typeof produced === 'string') {
    const completed = snapshot?.segments_completed
    const planned = snapshot?.segments_planned
    const truncated =
      typeof completed === 'number' && typeof planned === 'number' && completed < planned
    return { text: truncated ? `${produced}…` : produced, truncated }
  }
  return { text: snapshot?.text ?? '', truncated: false }
}
