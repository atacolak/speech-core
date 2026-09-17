import type { RunItem } from '@/lib/api'

export type TakeTranscript = { text: string; truncated: boolean }

const ESTIMATED_CHARS_PER_S = 15

/**
 * The text a take card may show for a settled run.
 *
 * Streamed runs provide `produced_text`, the exact text prefix that became
 * audio. Older one-shot runs have no produced prefix and fall back to the
 * requested text; partial or stopped runs use a deliberately rough estimate.
 *
 * Old one-shot runs carry no produced prefix; they fall back to the requested
 * Say text without a stop marker, because nothing is known to be missing.
 */
export function takeTranscript(run: RunItem): TakeTranscript {
  const snapshot = run.request_snapshot
  const duration = run.duration_s
  const completed = snapshot?.segments_completed
  const planned = snapshot?.segments_planned
  const incomplete =
    typeof completed === 'number' && typeof planned === 'number' && completed < planned
  const stopped = snapshot?.stopped === true
  const truncated =
    incomplete || (stopped && (completed == null || planned == null))

  const produced = snapshot?.produced_text
  const requested = snapshot?.text
  if (typeof produced === 'string' && produced.length > 0) {
    const overclaim = Boolean(requested && produced === requested && truncated)
    if (overclaim && typeof duration === 'number' && duration > 0) {
      return clipEstimatedText(produced, duration)
    }
    return { text: truncated ? `${produced} --` : produced, truncated }
  }

  if (requested && truncated && typeof duration === 'number' && duration > 0) {
    return clipEstimatedText(requested, duration)
  }

  if (requested) {
    return { text: truncated ? `${requested} --` : requested, truncated }
  }

  return { text: '', truncated: false }
}

function clipEstimatedText(text: string, duration: number | null): TakeTranscript {
  const estimatedLimit = Math.max(
    1,
    Math.floor((typeof duration === 'number' ? duration : 0) * ESTIMATED_CHARS_PER_S),
  )
  const limit = estimatedLimit
  let prefix = text.slice(0, Math.min(limit, text.length)).trimEnd()
  const boundary = prefix.search(/\s[^\s]*$/)
  if (boundary > 0 && limit < text.length) {
    prefix = prefix.slice(0, boundary).trimEnd()
  }
  return { text: `${prefix} --`, truncated: true }
}
