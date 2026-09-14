import { ApiError, apiUrl } from '@/lib/api'

/** A SOURCE is ingested media: a local file or a URL. It is not a voice. */
export type MediaSourceKind = 'file' | 'youtube'

export type SourceRange = {
  start_s: number
  end_s: number
}

/** Speaker ids stay source-local (`S1`) until the operator maps them onto a voice. */
export type SourceSpeaker = {
  local_id: string
  label: string
  duration_s: number
  mapped_voice_id: string | null
}

/** One analyzed turn on the source timeline. `overlap` turns are marked, never clipped. */
export type SourceTurn = {
  speaker_id: string
  start_s: number
  end_s: number
  text?: string
  overlap?: boolean
}

export type SourceOverlap = SourceRange & { speakers: string[] }

export type SourceAnalysis = {
  id: string
  start_s: number
  end_s: number
  processor: string
  model_id: string | null
  config: Record<string, unknown>
  result: { segments?: SourceTurn[]; overlaps?: SourceOverlap[] }
  created_at: string
}

/** A CLIP is one speaker's turns cropped out of a source; the mapped voice owns it. */
export type SourceClip = {
  id: string
  source_id: string
  speaker_local_id: string
  voice_id: string | null
  ranges: SourceRange[]
  segments: SourceTurn[]
  audio_artifact_id: string
  clean_transcript: string
  created_at: string
  source_title?: string
  source_kind?: MediaSourceKind
}

export type MediaSource = {
  id: string
  kind: MediaSourceKind
  /** Where the audio came from: the uploaded filename, or the URL. */
  origin: string
  title: string
  audio_artifact_id: string
  waveform_artifact_id: string | null
  duration_s: number
  meta: Record<string, unknown>
  created_at: string
  /** The analyzed ranges, already merged by the backend: what the coverage bar paints. */
  coverage: SourceRange[]
  analyses: SourceAnalysis[]
  speakers: SourceSpeaker[]
  clips: SourceClip[]
}

/** ANALYZE takes a range, or `all` — never a half-specified one. */
export type AnalyzeRequest = { all: true } | ({ all?: false } & SourceRange)

export type AnalyzeResult = MediaSource & { analyses_added: number }

export type ExtractRequest = {
  speaker_local_id: string
  ranges: SourceRange[]
}

async function sourceRequest(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(apiUrl(path), init)
  if (!response.ok) {
    const text = await response.text()
    let detail: unknown = text
    try {
      const parsed: unknown = JSON.parse(text)
      detail = parsed && typeof parsed === 'object' && 'detail' in parsed ? parsed.detail : parsed
    } catch {
      detail = text
    }
    throw new ApiError(`sources ${response.status}`, response.status, detail)
  }
  if (response.status === 204) {
    return null
  }
  return (await response.json()) as unknown
}

export async function fetchSources(): Promise<MediaSource[]> {
  const body = (await sourceRequest('/api/sources')) as { items: MediaSource[] }
  return body.items
}

/** Both ingests post the same form. The URL form downloads audio (no ASR, no analyze). */
export async function addSourceFile(file: File, title?: string): Promise<MediaSource> {
  const form = new FormData()
  form.append('file', file)
  if (title) {
    form.append('title', title)
  }
  return (await sourceRequest('/api/sources', { method: 'POST', body: form })) as MediaSource
}

export async function addSourceUrl(url: string): Promise<MediaSource> {
  const form = new FormData()
  form.append('url', url)
  return (await sourceRequest('/api/sources', { method: 'POST', body: form })) as MediaSource
}

export async function analyzeSource(
  sourceId: string,
  range: AnalyzeRequest,
): Promise<AnalyzeResult> {
  return (await sourceRequest(`/api/sources/${sourceId}/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(range),
  })) as AnalyzeResult
}

/** Cropping is the one place a voice comes out of a source. Overlaps are refused, not merged. */
export async function extractClips(
  sourceId: string,
  request: ExtractRequest,
): Promise<SourceClip> {
  return (await sourceRequest(`/api/sources/${sourceId}/extract`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })) as SourceClip
}

export function sourceAudioUrl(sourceId: string): string {
  return apiUrl(`/api/sources/${sourceId}/audio`)
}

/** Cheap peaks artifact written at ingest. Missing or unreadable → empty, never a fake wave. */
export async function fetchPeaks(artifactId: string): Promise<number[]> {
  const response = await fetch(apiUrl(`/api/artifacts/${artifactId}/audio`))
  if (!response.ok) {
    return []
  }
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'peaks' in body && Array.isArray(body.peaks)) {
      return body.peaks.map((value) => Number(value)).filter((value) => Number.isFinite(value))
    }
  } catch {
    return []
  }
  return []
}


/** A voice's clips ride along with its profile; read them without widening the Voice type. */
export function voiceClips(voice: object): SourceClip[] {
  if (!('clips' in voice)) {
    return []
  }
  const raw: unknown = voice.clips
  if (!Array.isArray(raw)) {
    return []
  }
  return raw.filter(
    (item): item is SourceClip =>
      item !== null &&
      typeof item === 'object' &&
      'id' in item &&
      typeof item.id === 'string' &&
      'audio_artifact_id' in item &&
      typeof item.audio_artifact_id === 'string',
  )
}

/** A missing analyzer is a 501 (or an unwired route: 404), never a fake diarization. */
export function isUnavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 501)
}
