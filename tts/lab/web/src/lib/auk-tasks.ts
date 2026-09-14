/**
 * The AuK cookbook: a task owns its instruction template. Chips fill the
 * instruction box; the operator edits the text before generating.
 *
 * Community ranking is a prior, not proof, so the less reliable tasks stay
 * visible but marked instead of hidden.
 */
export type AukPrecision = 'bf16' | 'int8'

export type AukPreset = {
  /** `auk_task` sent to the lab. */
  id: string
  /** Chip text; also the operator word for a candidate produced by this task. */
  label: string
  /** Cookbook instruction. `{text}` is filled from the transcript. */
  template: string
  confidence: 'confident' | 'less-reliable'
}

/** Task defaults owned by the preset, not by the operator surface. */
export const AUK_SETTINGS_DEFAULTS = { nfe: 32, cfg: 2, sway: -1 }

export const AUK_PRESETS: readonly AukPreset[] = [
  {
    id: 'enhance',
    label: 'enhance',
    template: 'Remove noise and reverberation while retaining the speech.',
    confidence: 'confident',
  },
  {
    id: 'denoise',
    label: 'denoise',
    template: "Denoise the recording while preserving the speaker's voice.",
    confidence: 'confident',
  },
  {
    id: 'repair',
    label: 'repair',
    template: 'Repair the quality of the speech while retaining the speaker.',
    confidence: 'confident',
  },
  {
    id: 'separate',
    label: 'separate',
    template: 'Keep the first speaker by talking order and remove the others.',
    confidence: 'confident',
  },
  {
    id: 'clone',
    label: 'clone',
    template: 'Say the following with the same voice: "{text}"',
    confidence: 'confident',
  },
  { id: 'volume', label: 'volume', template: 'Increase the volume by 5 dB.', confidence: 'confident' },
  {
    id: 'target_speaker',
    label: 'target speaker',
    template: "Keep the speaker who says '{text}'.",
    confidence: 'less-reliable',
  },
  { id: 'emotion', label: 'emotion', template: 'Change the emotion to: ', confidence: 'less-reliable' },
  { id: 'timbre', label: 'timbre', template: 'Change the timbre to: ', confidence: 'less-reliable' },
  { id: 'whisper', label: 'whisper', template: 'Whisper the speech.', confidence: 'less-reliable' },
  { id: 'accent', label: 'accent', template: 'Speak with this accent: ', confidence: 'less-reliable' },
  {
    id: 'nonverbal',
    label: 'nonverbal',
    template: 'Add this nonverbal sound: ',
    confidence: 'less-reliable',
  },
  { id: 'speed', label: 'speed', template: 'Change the speed: ', confidence: 'less-reliable' },
  { id: 'pitch', label: 'pitch', template: 'Change the pitch: ', confidence: 'less-reliable' },
  { id: 'content_edit', label: 'content', template: 'Edit the content: ', confidence: 'less-reliable' },
]

/** Operator intent: what the reference is being built for, in the order it is usually reached for. */
export type AukIntentId = 'CLEAN' | 'ISOLATE' | 'PERFORM' | 'EDIT' | 'SYNTHESIZE'

export type AukIntent = {
  id: AukIntentId
  /** One line of what the intent asks for; the instruction box stays the escape hatch. */
  hint: string
  /** Cookbook task ids. Every preset is filed under exactly one intent. */
  tasks: readonly string[]
}

export const AUK_INTENTS: readonly AukIntent[] = [
  {
    id: 'CLEAN',
    hint: 'Take the room, the noise and the level off the take.',
    tasks: ['enhance', 'denoise', 'repair', 'volume'],
  },
  {
    id: 'ISOLATE',
    hint: 'Keep one speaker and drop the rest.',
    tasks: ['separate', 'target_speaker'],
  },
  {
    id: 'PERFORM',
    hint: 'Change how it is said, not what is said.',
    tasks: ['emotion', 'timbre', 'whisper', 'accent', 'nonverbal', 'speed', 'pitch'],
  },
  {
    id: 'EDIT',
    hint: 'Change the words themselves.',
    tasks: ['content_edit'],
  },
  {
    id: 'SYNTHESIZE',
    hint: 'Say something new in this voice.',
    tasks: ['clone'],
  },
]

export const AUK_PRECISIONS: readonly { value: AukPrecision; label: string }[] = [
  { value: 'bf16', label: 'bf16' },
  { value: 'int8', label: 'int8' },
]

/** bf16 is the quality pin. int8 is a second, explicit choice — never a silent downgrade. */
export const DEFAULT_AUK_PRECISION: AukPrecision = 'bf16'

export function aukInstruction(preset: AukPreset, transcript: string): string {
  return preset.template.replaceAll('{text}', transcript.trim())
}

/** Operator word for a candidate: its task when known, "candidate" when the id is empty. */
export function aukTaskWord(aukTask: string | null | undefined): string {
  if (!aukTask) {
    return 'candidate'
  }
  return AUK_PRESETS.find((preset) => preset.id === aukTask)?.label ?? aukTask
}
