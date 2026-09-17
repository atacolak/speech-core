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
  const partial =
    snapshot?.stopped === true ||
    (typeof completed === 'number' && typeof planned === 'number' && completed < planned)

  const alignment = run.alignment
  if (alignment?.status === 'ready' && alignment.words?.length && typeof duration === 'number') {
    const kept = alignment.words.filter((word) => word.start_s < duration)
    if (kept.length) {
      const text = kept.map((word) => word.text).join(' ')
      const last = kept[kept.length - 1]
      const truncated = partial || last.end_s < duration
      return { text: truncated ? `${text}…` : text, truncated }
    }
  }

  const produced = snapshot?.produced_text
  if (typeof produced === 'string' && produced.length > 0) {
    return { text: partial ? `${produced}…` : produced, truncated: partial }
  }

  const requested = snapshot?.text
  if (typeof requested === 'string' && requested.length > 0) {
    if (partial) {
      return clipEstimatedText(requested, duration)
    }
    return { text: requested, truncated: false }
  }

  return { text: '', truncated: false }
}

function clipEstimatedText(text: string, duration: number | null): TakeTranscript {
  const estimatedLimit = Math.max(
    1,
    Math.floor((typeof duration === 'number' ? duration : 0) * ESTIMATED_CHARS_PER_S),
  )
  const limit = Math.min(estimatedLimit, Math.max(1, text.length - 1))
  let prefix = text.slice(0, limit).trimEnd()
  const boundary = prefix.search(/\s[^\s]*$/)
  if (boundary > 0) {
    prefix = prefix.slice(0, boundary).trimEnd()
  }
  return { text: `${prefix || text.slice(0, limit)}…`, truncated: true }
}
