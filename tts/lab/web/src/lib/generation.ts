import type { GenerationBody, Voice } from '@/lib/api'

export type GenerationState = {
  cfg: number
  seed: number
  temperature: number
  depthTemperature: number
  doSample: boolean
  topK: number
  topP: number
  maxNewTokens: number
  dual: boolean
  cfgRef: number
  cfgIns: number
}

export const DEFAULT_GENERATION: GenerationState = {
  cfg: 1,
  seed: 42,
  temperature: 0.9,
  depthTemperature: 0.9,
  doSample: true,
  topK: 50,
  topP: 1,
  maxNewTokens: 750,
  dual: false,
  cfgRef: 1,
  cfgIns: 1,
}

export const DEFAULT_TAKE_LIMIT = 5
export const MAX_TAKE_LIMIT = 50

export function clampTakeLimit(value: number): number {
  if (!Number.isFinite(value)) {
    return DEFAULT_TAKE_LIMIT
  }
  return Math.min(MAX_TAKE_LIMIT, Math.max(1, Math.round(value)))
}

export function toGenerationBody(state: GenerationState): GenerationBody {
  return {
    guidance: state.dual
      ? { mode: 'dual', reference: state.cfgRef, instruction: state.cfgIns }
      : { mode: 'single', cfg: state.cfg },
    seed: state.seed,
    temperature: state.temperature,
    depth_temperature: state.depthTemperature,
    do_sample: state.doSample,
    top_k: state.topK,
    top_p: state.topP,
    max_new_tokens: state.maxNewTokens,
  }
}

export function fromStoredGeneration(raw: Voice['generation'] | null | undefined): GenerationState {
  if (!raw) {
    return { ...DEFAULT_GENERATION }
  }
  const guidance = raw.guidance
  const dual = Boolean(guidance && guidance.mode === 'dual')
  return {
    cfg:
      guidance && guidance.mode === 'single' && typeof guidance.cfg === 'number'
        ? guidance.cfg
        : DEFAULT_GENERATION.cfg,
    seed: typeof raw.seed === 'number' ? raw.seed : DEFAULT_GENERATION.seed,
    temperature:
      typeof raw.temperature === 'number' ? raw.temperature : DEFAULT_GENERATION.temperature,
    depthTemperature:
      typeof raw.depth_temperature === 'number'
        ? raw.depth_temperature
        : DEFAULT_GENERATION.depthTemperature,
    doSample: typeof raw.do_sample === 'boolean' ? raw.do_sample : DEFAULT_GENERATION.doSample,
    topK: typeof raw.top_k === 'number' ? raw.top_k : DEFAULT_GENERATION.topK,
    topP: typeof raw.top_p === 'number' ? raw.top_p : DEFAULT_GENERATION.topP,
    maxNewTokens:
      typeof raw.max_new_tokens === 'number' ? raw.max_new_tokens : DEFAULT_GENERATION.maxNewTokens,
    dual,
    cfgRef:
      dual && guidance && guidance.mode === 'dual' && typeof guidance.reference === 'number'
        ? guidance.reference
        : DEFAULT_GENERATION.cfgRef,
    cfgIns:
      dual && guidance && guidance.mode === 'dual' && typeof guidance.instruction === 'number'
        ? guidance.instruction
        : DEFAULT_GENERATION.cfgIns,
  }
}
