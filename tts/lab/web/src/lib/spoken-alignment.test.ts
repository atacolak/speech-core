import { describe, expect, it } from 'vitest'
import type { RunAlignment } from '@/lib/api'
import { alignSayWords, highlightAt } from '@/lib/spoken-alignment'

const ALIGNED_TITLE =
  'Aligned from the produced take with Parakeet; Breeze supplies no word timestamps.'
const ESTIMATED_TITLE =
  "Estimated from the previous aligned take's speech rate; until this take is aligned, error may span the whole take."
const LIVE_ESTIMATED_TITLE =
  "Estimated from this take's current duration; until the take settles, error may span the whole take."

const SAY = 'Hello, brave world.'

/** Punctuation-mismatched Parakeet words on a deliberately uneven cadence. */
const READY: RunAlignment = {
  status: 'ready',
  text: '“hello” Brave world…',
  words: [
    { text: '“hello”', start_s: 0.2, end_s: 0.5 },
    { text: 'Brave', start_s: 2.6, end_s: 2.9 },
    { text: 'world…', start_s: 2.95, end_s: 3.2 },
  ],
}

describe('alignSayWords', () => {
  it('matches punctuation-mismatched words monotonically', () => {
    expect(alignSayWords(SAY, READY.words ?? [])).toEqual([
      { text: '“hello”', start_s: 0.2, end_s: 0.5, wordIndex: 0 },
      { text: 'Brave', start_s: 2.6, end_s: 2.9, wordIndex: 1 },
      { text: 'world…', start_s: 2.95, end_s: 3.2, wordIndex: 2 },
    ])
  })

  it('skips filler words and unspoken Say words without reordering Say', () => {
    expect(
      alignSayWords('one two three', [
        { text: 'one', start_s: 0, end_s: 0.2 },
        { text: 'uh', start_s: 0.2, end_s: 0.3 },
        { text: 'three', start_s: 0.4, end_s: 0.6 },
      ]).map((word) => word.wordIndex),
    ).toEqual([0, 2])

    expect(
      alignSayWords('alpha beta', [
        { text: 'beta', start_s: 0, end_s: 0.1 },
        { text: 'alpha', start_s: 0.2, end_s: 0.3 },
      ]).map((word) => word.wordIndex),
    ).toEqual([0])
  })

  it('matches nothing when the heard words are only punctuation', () => {
    expect(alignSayWords('one two', [{ text: '—', start_s: 0, end_s: 0.1 }])).toEqual([])
  })
})

describe('highlightAt', () => {
  it('uses the Parakeet interval that contains the playhead', () => {
    expect(highlightAt(0.3, SAY, READY, null)).toEqual({
      wordIndex: 0,
      mode: 'aligned',
      title: ALIGNED_TITLE,
    })
    expect(highlightAt(2.7, SAY, READY, null)).toEqual({
      wordIndex: 1,
      mode: 'aligned',
      title: ALIGNED_TITLE,
    })
  })

  it('highlights nothing between aligned words instead of inventing a timestamp', () => {
    expect(highlightAt(1.8, SAY, READY, null)).toBeNull()
    expect(highlightAt(4, SAY, READY, null)).toBeNull()
    expect(highlightAt(0.1, SAY, READY, null)).toBeNull()
  })

  it('labels the prior-rate fallback while alignment is pending', () => {
    expect(highlightAt(1, 'one two three', { status: 'pending' }, 4)).toEqual({
      wordIndex: 1,
      mode: 'estimated',
      title: ESTIMATED_TITLE,
    })
  })

  it('labels the prior-rate fallback for an unavailable alignment', () => {
    expect(highlightAt(0.1, 'one two', { status: 'unavailable' }, 4)).toEqual({
      wordIndex: 0,
      mode: 'estimated',
      title: ESTIMATED_TITLE,
    })
  })
  it('estimates from the live take duration instead of the prior rate', () => {
    expect(highlightAt(0.5, 'one two three', { status: 'pending' }, 6, 10)).toEqual({
      wordIndex: 0,
      mode: 'estimated',
      title: LIVE_ESTIMATED_TITLE,
    })
    expect(highlightAt(0.5, 'one two three', { status: 'pending' }, 6, 0)).toBeNull()
  })

  it('uses the live estimate before a ready alignment from the prior take', () => {
    expect(
      highlightAt(
        0.5,
        'one two three',
        {
          status: 'ready',
          words: [
            { text: 'one', start_s: 0, end_s: 0.2 },
            { text: 'two', start_s: 0.4, end_s: 0.6 },
          ],
        },
        6,
        10,
      ),
    ).toEqual({
      wordIndex: 0,
      mode: 'estimated',
      title: LIVE_ESTIMATED_TITLE,
    })
  })

  it('returns null when no prior aligned rate exists', () => {
    expect(highlightAt(1, 'one two', { status: 'unavailable' }, null)).toBeNull()
    expect(highlightAt(1, 'one two', { status: 'pending' }, null)).toBeNull()
  })

  it('clips the estimate to the text it was given', () => {
    expect(highlightAt(99, 'one two', { status: 'unavailable' }, 4)).toEqual({
      wordIndex: 1,
      mode: 'estimated',
      title: ESTIMATED_TITLE,
    })
    expect(highlightAt(0.5, '', { status: 'pending' }, 4)).toBeNull()
  })
})
