import type { RunItem } from '@/lib/api'

export type TakeTranscript = { text: string; truncated: boolean }

const ESTIMATED_CHARS_PER_S = 15

/**
 * The text a take card may show for a settled run.
 *
 * Ready CPU alignment clips the transcript to the audio duration. When that
 * is unavailable, `produced_text` is the best known prefix. A stopped or
 * partial run without either uses a deliberately rough character estimate;
 * this is not a word clock.
 *
 * Old one-shot runs carry no produced prefix; they fall back to the requested
 * Say text with no ellipsis, because nothing is known to be missing.
 */
export function takeTranscript(run: RunItem): TakeTranscript {
  const snapshot = run.request_snapshot
  const duration = run.duration_s
  const completed = snapshot?.segments_completed
  const planned = snapshot?.segments_planned
  const incomplete =
    typeof completed === 'number' && typeof planned === 'number' && completed < planned
  const knownShort =
    incomplete ||
    (snapshot?.stopped === true && (completed == null || planned == null))

  const alignment = run.alignment
  if (alignment?.status === 'ready' && alignment.words?.length && typeof duration === 'number') {
    const kept = alignment.words.filter((word) => word.start_s < duration)
    if (kept.length) {
      const text = kept.map((word) => word.text).join(' ')
      const truncated = kept.length < alignment.words.length || incomplete
      return { text: truncated ? `${text}…` : text, truncated }
    }
  }

  const produced = snapshot?.produced_text
  const requested = snapshot?.text
  if (typeof produced === 'string' && produced.length > 0) {
    const overclaim = Boolean(requested && produced === requested && knownShort)
    if (overclaim && typeof duration === 'number' && duration > 0) {
      return clipEstimatedText(produced, duration)
    }
    return { text: incomplete ? `${produced}…` : produced, truncated: incomplete }
  }

  if (requested && knownShort && typeof duration === 'number' && duration > 0) {
    return clipEstimatedText(requested, duration)
  }

  if (requested) {
    return { text: requested, truncated: false }
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
  return { text: `${prefix}…`, truncated: true }
}
