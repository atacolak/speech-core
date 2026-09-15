import { describe, expect, it } from 'vitest'
import { DEFAULT_GENERATION, fromStoredGeneration } from '@/lib/generation'

const stored = {
  guidance: { mode: 'single' as const, cfg: 4 },
  seed: 42,
}

describe('generation defaults', () => {
  it('uses cfg 1 only when single guidance is unset', () => {
    expect(DEFAULT_GENERATION.cfg).toBe(1)
    expect(fromStoredGeneration(null).cfg).toBe(1)
    expect(fromStoredGeneration(stored).cfg).toBe(4)
  })

  it('keeps dual defaults at unity', () => {
    expect(DEFAULT_GENERATION.cfgRef).toBe(1)
    expect(DEFAULT_GENERATION.cfgIns).toBe(1)
  })
})
