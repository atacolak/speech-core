import type { RunAlignment } from '@/lib/api'

export type AlignedWord = { text: string; start_s: number; end_s: number }

export type Highlight = { wordIndex: number; mode: 'aligned' | 'estimated'; title: string }

/**
 * The only two statements this module is allowed to make about timing.
 *
 * Breeze emits raw PCM and no word timestamps, so a highlight is either
 * Parakeet's real interval for the produced take, or an explicitly-labelled
 * estimate from the previous aligned take's speech rate.
 */
export const ALIGNED_TITLE =
  'Aligned from the produced take with Parakeet; Breeze supplies no word timestamps.'

export const ESTIMATED_TITLE =
  "Estimated from the previous aligned take's speech rate; until this take is aligned, error may span the whole take."

export const LIVE_ESTIMATED_TITLE =
  "Estimated from this take's current duration; until the take settles, error may span the whole take."

/** Surrounding Unicode punctuation and symbols are noise; interior spelling is not. */
const SURROUNDING_NOISE = /^[\p{P}\p{S}]+|[\p{P}\p{S}]+$/gu

function normalizeWord(text: string): string {
  return text.toLowerCase().replace(SURROUNDING_NOISE, '')
}

function sayWords(say: string): string[] {
  return say.split(/\s+/).filter((word) => word.length > 0)
}

function sayCharacterCount(say: string): number {
  return sayWords(say).reduce((total, word) => total + word.length, 0)
}

/**
 * Map the words Parakeet heard back onto the Say words they came from.
 *
 * Matching is monotonic: Say is walked in order and each heard word is consumed
 * once, so a filler word or a skipped Say word shifts the rest without ever
 * reordering the text. The heard word keeps its own spelling and interval; only
 * the Say index is added.
 */
export function alignSayWords(
  say: string,
  heard: AlignedWord[],
): Array<AlignedWord & { wordIndex: number }> {
  const matched: Array<AlignedWord & { wordIndex: number }> = []
  let cursor = 0
  const words = sayWords(say)
  for (let wordIndex = 0; wordIndex < words.length; wordIndex += 1) {
    const wanted = normalizeWord(words[wordIndex])
    if (!wanted) {
      continue
    }
    for (let index = cursor; index < heard.length; index += 1) {
      if (normalizeWord(heard[index].text) === wanted) {
        matched.push({ ...heard[index], wordIndex })
        cursor = index + 1
        break
      }
    }
  }
  return matched
}

/**
 * The word under the playhead, or nothing.
 *
 * A ready alignment is authoritative: the playhead must fall inside a real
 * Parakeet interval, and the gap between words stays unhighlighted rather than
 * inventing a timestamp. Without a ready alignment the only fallback is the
 * prior aligned take's character rate, labelled as the estimate it is; no rate
 * means no highlight at all.
 */
export function highlightAt(
  playheadS: number,
  say: string,
  alignment: RunAlignment | null,
  priorAlignedCharsPerSecond: number | null,
  liveDurationS: number | null = null,
): Highlight | null {
  if (liveDurationS !== null) {
    const characters = sayCharacterCount(say)
    const liveRate = liveDurationS > 0 && characters > 0 ? characters / liveDurationS : null
    return estimateAt(playheadS, say, liveRate, LIVE_ESTIMATED_TITLE)
  }
  const heard = alignment?.words ?? []
  if (alignment?.status === 'ready' && heard.length > 0) {
    const match = alignSayWords(say, heard).find(
      (word) => playheadS >= word.start_s && playheadS <= word.end_s,
    )
    return match ? { wordIndex: match.wordIndex, mode: 'aligned', title: ALIGNED_TITLE } : null
  }
  return estimateAt(playheadS, say, priorAlignedCharsPerSecond, ESTIMATED_TITLE)
}

function estimateAt(
  playheadS: number,
  say: string,
  charsPerSecond: number | null,
  title: string,
): Highlight | null {
  const rate = charsPerSecond
  if (rate === null || !Number.isFinite(rate) || rate <= 0 || !Number.isFinite(playheadS)) {
    return null
  }
  const words = sayWords(say)
  const characters = sayCharacterCount(say)
  if (characters === 0) {
    return null
  }
  const position = Math.min(Math.max(playheadS, 0) * rate, characters)
  let consumed = 0
  for (let index = 0; index < words.length; index += 1) {
    consumed += words[index].length
    if (position < consumed) {
      return { wordIndex: index, mode: 'estimated', title }
    }
  }
  return { wordIndex: words.length - 1, mode: 'estimated', title }
}
