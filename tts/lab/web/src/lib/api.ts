import type { AukPrecision } from '@/lib/auk-tasks'

export type RuntimeState =
  | 'unloaded'
  | 'loading'
  | 'ready'
  | 'unloading'
  | 'insufficient_vram'
  | 'error'

export type RuntimeErrorInfo = {
  code: string
  message?: string
  required_bytes?: number
  free_bytes?: number
  shortfall_bytes?: number
}

export type RuntimeInfo = {
  selected: string
  status: string
  state: RuntimeState
  leftover_parked: boolean
  not_a_pin_swap: boolean
  display_name?: string
  engine?: string
  voicecat_path: boolean
  implementation?: string
  pin_commit?: string
  qual_root?: string
  worker_pid?: number | null
  required_vram_bytes?: number
  vram_margin_bytes?: number
  free_vram_bytes?: number | null
  used_vram_bytes?: number | null
  last_error?: RuntimeErrorInfo | null
  loaded_at?: string | null
  model_revision?: string | null
  gpu_index?: number
  load_phase?: string | null
  load_started_at?: string | null
  load_elapsed_s?: number | null
  active_voice_id?: string | null
  live_call_active?: boolean
  live_call_remaining_s?: number
  live_call_holder?: 'session' | 'hop' | null
  processor?: string | null
}

export type Interval = {
  start_s: number
  end_s: number
}

export type AukSettings = {
  nfe?: number
  cfg?: number
  sway?: number
}

export type ReferenceVariant = {
  id: string
  voice_profile_id: string
  kind: 'original' | 'resemble' | 'auk' | 'other' | string
  audio_artifact_id: string
  duration_s: number
  processor_cache_key?: string | null
  stale?: boolean
  /** Lineage of an auk candidate. */
  parent_variant_id?: string | null
  auk_task?: string | null
  instruction?: string | null
  model_variant?: string | null
  auk_precision?: AukPrecision | null
  encoder_precision?: string | null
  seed?: number | null
  settings?: AukSettings | null
  approved?: boolean
}

/** A voice profile's immutable origin audio: many per voice, never overwritten. */
export type VoiceSource = {
  id: string
  label: string
  artifact_id: string
  transcript?: string | null
  duration_s?: number | null
  keep_intervals?: Interval[]
}

/** An experiment (candidate) or an approved reference, with the lineage that produced it. */
export type VoiceArtifact = {
  id: string
  role: 'experiment' | 'reference'
  /** `original`, `auk`, or a parked specialist such as `resemble`. */
  kind: 'original' | 'resemble' | 'auk' | 'other' | string
  name?: string | null
  audio_artifact_id: string
  parent_id?: string | null
  source_id?: string | null
  /** The crop this artifact was rendered from. */
  keep_intervals?: Interval[]
  auk_task?: string | null
  instruction?: string | null
  model_variant?: string | null
  auk_precision?: AukPrecision | null
  seed?: number | null
  settings?: AukSettings | null
  approved?: boolean
  approved_at?: string | null
  tags?: string[]
  default?: boolean
  stale?: boolean
  created_at?: string
}

export type SpeakerSegment = {
  speaker_id: string
  start_s: number
  end_s: number
  text?: string
  overlap?: boolean
}

export type SpeakerInfo = {
  id: string
  label?: string
  duration_s: number
}

export type SpeakerOverlap = {
  start_s: number
  end_s: number
  speakers: string[]
}

export type SpeakerAnalysis = {
  id: string
  speakers: SpeakerInfo[]
  segments: SpeakerSegment[]
  overlaps: SpeakerOverlap[]
  stale: boolean
  model_id?: string | null
  model_revision?: string | null
  cache_key?: string | null
  peak_vram_bytes?: number | null
}

export type Voice = {
  id: string
  name: string
  tags: string[]
  source_audio_artifact_id: string
  original_artifact_id?: string
  original_format?: string
  source_transcript: string
  keep_intervals: Interval[]
  effective_transcript: string
  source_words?: Array<{ text: string; start_s: number; end_s: number }>
  transcript_locked?: boolean
  active_reference_variant_id: string | null
  active_variant?: ReferenceVariant | null
  variants?: ReferenceVariant[]
  /** Profile shape: many sources, role-carrying artifacts, one optional default reference. */
  sources?: VoiceSource[]
  artifacts?: VoiceArtifact[]
  default_reference_id?: string | null
  speaker_analysis?: SpeakerAnalysis | null
  duration_s?: number | null
  source_duration_s?: number | null
  effective_duration_s?: number | null
  generation?: GenerationBody | null
  take_limit?: number
  created_at: string
  updated_at: string
  transcribe?: { text: string; wall_s: number }
}

export type RunItem = {
  id: string
  voice_id: string
  request_snapshot?: {
    text?: string
    steer?: string
    synthesis_text?: string
    generation?: Record<string, unknown>
    voice_profile_id?: string
  }
  output_artifact_id: string
  latency_ms: number | null
  first_audio_ms: number | null
  duration_s: number | null
  rating: string | null
  tags: string[]
  created_at?: string
}

export type Take = {
  id: string
  output_artifact_id: string
  duration_s: number
  latency_ms: number
  first_audio_ms: number | null
  request_snapshot?: RunItem['request_snapshot']
  voice_id?: string
}

export type SteerFixture = {
  id: string
  text: string
  steer: string
}

export type GenerationBody = {
  guidance: { mode: 'single'; cfg: number } | { mode: 'dual'; reference: number; instruction: number }
  seed: number
  temperature?: number
  depth_temperature?: number
  do_sample?: boolean
  top_k?: number
  top_p?: number
  max_new_tokens?: number
}

export type VoiceGeneration = GenerationBody

export type GenerateSegment = {
  index: number
  text: string
  state: 'pending' | 'queued' | 'generating' | 'generated' | 'cancelled' | 'error'
  duration_s: number | null
  audio_url: string | null
}

export type GenerateJob = {
  id: string
  state: 'running' | 'complete' | 'cancelled' | 'error'
  cursor: number
  lookahead: number
  blocked_on_live_call: boolean
  run_id: string | null
  output_artifact_id: string | null
  error: string | null
  segments: GenerateSegment[]
}

/** `/api/generate` takes the same body as `/api/synthesize`; only the response differs. */
export type GenerateInput = {
  text: string
  steer?: string
  synthesis_text?: string
  voice_profile_id: string
  reference_variant_id?: string
  generation?: GenerationBody
}

export class ApiError extends Error {
  status: number
  code?: string
  detail: unknown
  constructor(label: string, status: number, detail: unknown) {
    super(`${label} ${status}`)
    this.status = status
    this.detail = detail
    if (detail && typeof detail === 'object' && 'code' in detail) {
      this.code = String((detail as { code: string }).code)
    }
  }
}

export function formatApiError(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = error.detail
    if (detail && typeof detail === 'object') {
      const rec = detail as { message?: unknown; code?: unknown }
      if (typeof rec.message === 'string' && rec.message.trim()) {
        return rec.message
      }
      if (typeof rec.code === 'string' && rec.code.trim()) {
        return rec.code
      }
    }
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}

async function readError(response: Response, label: string): Promise<ApiError> {
  const text = await response.text()
  let detail: unknown = text
  try {
    detail = JSON.parse(text) as unknown
    if (detail && typeof detail === 'object' && 'detail' in detail) {
      detail = (detail as { detail: unknown }).detail
    }
  } catch {
    detail = text
  }
  return new ApiError(label, response.status, detail)
}

export function apiUrl(path: string): string {
  const relative = path.replace(/^\//, '')
  if (typeof window === 'undefined') {
    return `/${relative}`
  }
  const dir = window.location.pathname.endsWith('/')
    ? window.location.pathname
    : `${window.location.pathname}/`
  const url = new URL(relative, `${window.location.origin}${dir}`)
  return `${url.pathname}${url.search}${url.hash}`
}

function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(apiUrl(path), init)
}

export function artifactAudioUrl(artifactId: string): string {
  return apiUrl(`/api/artifacts/${artifactId}/audio`)
}

export function voiceReferenceAudioUrl(voiceId: string, stamp?: string): string {
  const query = stamp ? `?t=${encodeURIComponent(stamp)}` : ''
  return apiUrl(`/api/voices/${voiceId}/reference/audio${query}`)
}

export function keepDurationS(keep: Interval[] | undefined): number {
  return (keep ?? []).reduce((sum, interval) => sum + Math.max(0, interval.end_s - interval.start_s), 0)
}

export function sourceDurationS(voice: Voice): number {
  return voice.source_duration_s ?? voice.duration_s ?? 0
}

export function effectiveDurationS(voice: Voice): number {
  return voice.effective_duration_s ?? keepDurationS(voice.keep_intervals)
}

export function isCroppedReference(voice: Voice): boolean {
  return sourceDurationS(voice) - effectiveDurationS(voice) > 0.05
}

/** Reference kinds are internal implementation names; operator chrome names the outcome. */
const REFERENCE_WORDS: Record<string, string> = {
  original: 'original',
  // AuK is parked: its variants stay readable, but never under the product's task words.
  auk: 'candidate',
  resemble: 'denoised',
}

export function activeReferenceLabel(voice: Voice | undefined): string {
  const variant = voice?.active_variant
  if (!variant || variant.kind === 'original') {
    return 'original'
  }
  const word = REFERENCE_WORDS[variant.kind] ?? variant.kind
  return variant.stale ? `${word} (stale)` : word
}

export async function fetchRuntime(): Promise<RuntimeInfo> {
  const response = await apiFetch('/api/runtime/e2')
  if (!response.ok) {
    throw await readError(response, 'runtime')
  }
  return (await response.json()) as RuntimeInfo
}

export async function loadE2(): Promise<RuntimeInfo> {
  const response = await apiFetch('/api/runtime/e2/load', { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'runtime')
  }
  return (await response.json()) as RuntimeInfo
}

export async function unloadE2(): Promise<RuntimeInfo> {
  const response = await apiFetch('/api/runtime/e2/unload', { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'runtime')
  }
  return (await response.json()) as RuntimeInfo
}

export async function setActiveVoice(voiceId: string | null): Promise<RuntimeInfo | null> {
  if (!voiceId) {
    const response = await apiFetch('/api/runtime/active-voice', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice_id: null }),
    })
    if (!response.ok) {
      throw await readError(response, 'runtime')
    }
    return (await response.json()) as RuntimeInfo
  }
  const response = await apiFetch('/api/talker/voice', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ voice_id: voiceId }),
  })
  if (response.status === 404) {
    return null
  }
  if (!response.ok) {
    throw await readError(response, 'runtime')
  }
  return (await response.json()) as RuntimeInfo
}

export function isSampleVoice(voice: Voice): boolean {
  return voice.id === 'vp_sample_george_hotz' || voice.tags.includes('sample')
}

export function pickDefaultVoice(voices: Voice[]): Voice | undefined {
  return voices.find(isSampleVoice) ?? voices[0]
}

export async function fetchVoices(): Promise<Voice[]> {
  const response = await apiFetch('/api/voices')
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  const body = (await response.json()) as { items: Voice[] }
  return body.items
}

export async function createVoice(input: {
  name: string
  transcript?: string
  audio: File
}): Promise<Voice> {
  const data = new FormData()
  data.set('name', input.name)
  data.set('transcript', input.transcript ?? '')
  data.set('audio', input.audio)
  const response = await apiFetch('/api/voices', { method: 'POST', body: data })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

/** t1 owns this route; the workbench fails closed until the lab serves it. */
export async function addVoiceSource(
  voiceId: string,
  input: { label?: string; audio: File },
): Promise<Voice> {
  const data = new FormData()
  data.set('label', input.label ?? 'take')
  data.set('audio', input.audio)
  const response = await apiFetch(`/api/voices/${voiceId}/sources`, { method: 'POST', body: data })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function patchVoice(
  voiceId: string,
  patch: {
    name?: string
    tags?: string[]
    source_transcript?: string
    effective_transcript?: string
    keep_intervals?: Interval[]
    notes?: string
    generation?: GenerationBody | null
    take_limit?: number
  },
): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function deleteVoice(voiceId: string): Promise<void> {
  const response = await apiFetch(`/api/voices/${voiceId}`, { method: 'DELETE' })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
}

export async function transcribeVoice(voiceId: string): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/transcribe`, { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function denoiseVoice(voiceId: string): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/reference/denoise`, { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function analyzeSpeakers(voiceId: string): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/speakers/analyze`, { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function useSpeaker(voiceId: string, speakerId: string): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/speakers/use`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speaker_id: speakerId }),
  })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function keepOnly(voiceId: string, interval: Interval): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/reference/keep-only`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(interval),
  })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function excludeInterval(voiceId: string, interval: Interval): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/reference/exclude`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(interval),
  })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function resetReference(voiceId: string): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/reference/reset`, { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function activateVariant(voiceId: string, variantId: string): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/reference/activate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ variant_id: variantId }),
  })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

/** AuK occupancy, as reported by the lab. `loaded` is per-weight, not per-state. */
export type AukRuntimeInfo = {
  state: RuntimeState
  status?: string
  precision?: string | null
  pin?: string | null
  occupant?: string | null
  engine?: string | null
  weights_present?: boolean
  live_call_active?: boolean
  loaded?: { encoder?: boolean; model?: boolean; vae?: boolean }
  last_error?: RuntimeErrorInfo | null
}

export type AukTaskRequest = {
  auk_task: string
  instruction: string
  parent_variant_id?: string
  seed?: number
  auk_precision?: AukPrecision
  settings?: AukSettings
}

export async function fetchAukRuntime(): Promise<AukRuntimeInfo> {
  const response = await apiFetch('/api/auk/runtime')
  if (!response.ok) {
    throw await readError(response, 'auk runtime')
  }
  return (await response.json()) as AukRuntimeInfo
}

/** Precision is always sent: the operator picked it, the lab never guesses down to int8. */
export async function loadAuk(precision: AukPrecision): Promise<AukRuntimeInfo> {
  const response = await apiFetch('/api/auk/runtime/load', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ precision }),
  })
  if (!response.ok) {
    throw await readError(response, 'auk runtime')
  }
  return (await response.json()) as AukRuntimeInfo
}

export async function unloadAuk(): Promise<AukRuntimeInfo> {
  const response = await apiFetch('/api/auk/runtime/unload', { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'auk runtime')
  }
  return (await response.json()) as AukRuntimeInfo
}

export async function runAukTask(voiceId: string, request: AukTaskRequest): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/auk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function approveAukCandidate(voiceId: string, variantId: string): Promise<Voice> {
  const response = await apiFetch(`/api/voices/${voiceId}/auk/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ variant_id: variantId }),
  })
  if (!response.ok) {
    throw await readError(response, 'voices')
  }
  return (await response.json()) as Voice
}

export async function fetchRuns(voiceId?: string | null): Promise<RunItem[]> {
  const suffix = voiceId ? `?voice_id=${encodeURIComponent(voiceId)}` : ''
  const response = await apiFetch(`/api/runs${suffix}`)
  if (!response.ok) {
    throw await readError(response, 'runs')
  }
  const body = (await response.json()) as { items: RunItem[] }
  return body.items
}

export async function rateRun(runId: string, rating: string): Promise<RunItem> {
  const response = await apiFetch(`/api/runs/${runId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating }),
  })
  if (!response.ok) {
    throw await readError(response, 'runs')
  }
  return (await response.json()) as RunItem
}

export async function deleteRun(runId: string): Promise<void> {
  const response = await apiFetch(`/api/runs/${runId}`, { method: 'DELETE' })
  if (!response.ok) {
    throw await readError(response, 'runs')
  }
}

export async function fetchSteerFixtures(): Promise<SteerFixture[]> {
  const response = await apiFetch('/api/fixtures/steer')
  if (!response.ok) {
    throw await readError(response, 'fixtures')
  }
  const body = (await response.json()) as { items: SteerFixture[] }
  return body.items
}

export async function planSteer(text: string): Promise<{ text: string; steer: string; synthesis_text?: string }> {
  const response = await apiFetch('/api/plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!response.ok) {
    throw await readError(response, 'plan')
  }
  return (await response.json()) as { text: string; steer: string; synthesis_text?: string }
}

export async function synthesize(input: {
  text: string
  steer?: string
  synthesis_text?: string
  voice_profile_id: string
  generation?: GenerationBody
}): Promise<Take> {
  const response = await apiFetch('/api/synthesize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!response.ok) {
    throw await readError(response, 'synthesize')
  }
  return (await response.json()) as Take
}

/** Progressive generate: one job, many segments, playable as they land. */
export async function startGenerate(input: GenerateInput): Promise<GenerateJob> {
  const response = await apiFetch('/api/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!response.ok) {
    throw await readError(response, 'generate')
  }
  return (await response.json()) as GenerateJob
}

export async function fetchGenerateJob(jobId: string): Promise<GenerateJob> {
  const response = await apiFetch(`/api/generate/${jobId}`)
  if (!response.ok) {
    throw await readError(response, 'generate status')
  }
  return (await response.json()) as GenerateJob
}

/** The highest contiguously completed segment; a lost report only throttles generation. */
export async function reportGenerateCursor(jobId: string, index: number): Promise<GenerateJob> {
  const response = await apiFetch(`/api/generate/${jobId}/cursor`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index }),
  })
  if (!response.ok) {
    throw await readError(response, 'generate cursor')
  }
  return (await response.json()) as GenerateJob
}

export async function cancelGenerate(jobId: string): Promise<GenerateJob> {
  const response = await apiFetch(`/api/generate/${jobId}/cancel`, { method: 'POST' })
  if (!response.ok) {
    throw await readError(response, 'generate cancel')
  }
  return (await response.json()) as GenerateJob
}

export function generateSegmentAudioUrl(jobId: string, index: number): string {
  return apiUrl(`/api/generate/${jobId}/segments/${index}/audio`)
}

export function formatBytes(value?: number | null): string {
  if (value == null || Number.isNaN(value)) {
    return '—'
  }
  const units = ['B', 'KiB', 'MiB', 'GiB']
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  const digits = unit === 0 ? 0 : 1
  return `${amount.toFixed(digits)} ${units[unit]}`
}
