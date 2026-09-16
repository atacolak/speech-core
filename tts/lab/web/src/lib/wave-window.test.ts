import { describe, expect, it } from 'vitest'
import { MAX_VISIBLE_S, followPlayhead, waveWindow, windowFraction } from '@/lib/wave-window'

describe('waveWindow', () => {
  it('uses the full width below the ten second cap', () => {
    expect(waveWindow(4, 9)).toEqual({ startS: 0, visibleS: 4, maxStartS: 0, scrollable: false })
  })

  it('caps the visible duration at ten seconds and scrolls the rest', () => {
    expect(MAX_VISIBLE_S).toBe(10)
    expect(waveWindow(21, 99)).toEqual({ startS: 11, visibleS: 10, maxStartS: 11, scrollable: true })
    expect(waveWindow(10, 0)).toEqual({ startS: 0, visibleS: 10, maxStartS: 0, scrollable: false })
    expect(waveWindow(10.5, 0)).toEqual({ startS: 0, visibleS: 10, maxStartS: 0.5, scrollable: true })
  })

  it('clamps a negative, too large, or non-finite start into the scrollable range', () => {
    expect(waveWindow(21, -4)).toEqual({ startS: 0, visibleS: 10, maxStartS: 11, scrollable: true })
    expect(waveWindow(21, Number.NaN)).toEqual({ startS: 0, visibleS: 10, maxStartS: 11, scrollable: true })
    expect(waveWindow(21, Number.POSITIVE_INFINITY)).toEqual({
      startS: 0,
      visibleS: 10,
      maxStartS: 11,
      scrollable: true,
    })
    expect(waveWindow(6, 2)).toEqual({ startS: 0, visibleS: 6, maxStartS: 0, scrollable: false })
    expect(waveWindow(Number.NaN, 3)).toEqual({ startS: 0, visibleS: 0, maxStartS: 0, scrollable: false })
    expect(waveWindow(-2, 3)).toEqual({ startS: 0, visibleS: 0, maxStartS: 0, scrollable: false })
  })
})

describe('followPlayhead', () => {
  it('leaves the window alone while the playhead is inside it', () => {
    const window = waveWindow(21, 0)
    expect(followPlayhead(window, 21, 5)).toEqual(window)
    expect(followPlayhead(window, 21, 10)).toEqual(window)
  })

  it('scrolls just enough to keep the playhead visible', () => {
    const window = waveWindow(21, 0)
    expect(followPlayhead(window, 21, 12)).toEqual({
      startS: 2,
      visibleS: 10,
      maxStartS: 11,
      scrollable: true,
    })
    expect(followPlayhead(window, 21, 21)).toEqual({
      startS: 11,
      visibleS: 10,
      maxStartS: 11,
      scrollable: true,
    })
    expect(followPlayhead(waveWindow(21, 11), 21, 3).startS).toBe(3)
  })

  it('does not re-anchor the window when the take keeps growing', () => {
    const window = followPlayhead(waveWindow(12, 0), 12, 2)
    expect(window.startS).toBe(0)
    expect(followPlayhead(window, 12.4, 9.6).startS).toBe(0)
    expect(followPlayhead(waveWindow(12.4, 0), 12.4, 5).visibleS).toBe(10)
  })
})

describe('windowFraction', () => {
  it('maps seconds inside the visible window onto 0..1', () => {
    const window = waveWindow(21, 4)
    expect(windowFraction(window, 4)).toBe(0)
    expect(windowFraction(window, 9)).toBeCloseTo(0.5)
    expect(windowFraction(window, 14)).toBe(1)
  })

  it('returns null for seconds outside the visible window', () => {
    const window = waveWindow(21, 4)
    expect(windowFraction(window, 3.9)).toBeNull()
    expect(windowFraction(window, 14.1)).toBeNull()
    expect(windowFraction(window, Number.NaN)).toBeNull()
    expect(windowFraction(waveWindow(0, 0), 0)).toBeNull()
  })
})
