import { describe, expect, it } from 'vitest'
import type { ConversationTurn } from '@/lib/api'
import { chosenTurn, groupTurns, speakerBefore, voiceChangeBefore } from '@/features/conversations/conversation-model'

const turn = (over: Partial<ConversationTurn>): ConversationTurn => ({
  id: 'ct_x', conversation_id: 'cv_1', msg_seq: 0, variation_seq: 0,
  role: 'assistant', text: '', audio_artifact_id: null, voice_id: 'v1',
  steer: null, generation: null, alignment: null, chosen: true,
  started_at: null, ended_at: null, ...over,
})

describe('conversation model', () => {
  it('folds variation rows under one slot ordered by variation_seq', () => {
    expect(groupTurns([
      turn({ id: 'a', msg_seq: 0 }),
      turn({ id: 'b', msg_seq: 0, variation_seq: 1, chosen: false }),
      turn({ id: 'c', msg_seq: 1, role: 'user', voice_id: null }),
    ]).map((m) => m.variations.map((v) => v.id))).toEqual([['a', 'b'], ['c']])

    expect(groupTurns([
      turn({ id: 'b', msg_seq: 0, variation_seq: 1, chosen: false }),
      turn({ id: 'a', msg_seq: 0, variation_seq: 0 }),
    ]).map((m) => m.variations.map((v) => v.id))).toEqual([['a', 'b']])
  })

  it('picks the flagged chosen variation', () => {
    const [message] = groupTurns([
      turn({ id: 'a', msg_seq: 0, chosen: false }),
      turn({ id: 'b', msg_seq: 0, variation_seq: 1, chosen: true }),
    ])
    expect(chosenTurn(message).id).toBe('b')
  })

  it('falls back to the first variation when none is flagged', () => {
    const [message] = groupTurns([
      turn({ id: 'a', msg_seq: 0, chosen: false }),
      turn({ id: 'b', msg_seq: 0, variation_seq: 1, chosen: false }),
    ])
    expect(chosenTurn(message).id).toBe('a')
  })

  it('returns the previous assistant voice when it differs, skipping user turns', () => {
    const messages = groupTurns([
      turn({ id: 'a', msg_seq: 0, voice_id: 'v1' }),
      turn({ id: 'b', msg_seq: 1, role: 'user', voice_id: null }),
      turn({ id: 'c', msg_seq: 2, voice_id: 'v2' }),
      turn({ id: 'd', msg_seq: 3, voice_id: 'v2' }),
    ])
    expect(voiceChangeBefore(messages, 2)).toBe('v1')
    expect(voiceChangeBefore(messages, 3)).toBeNull()
    expect(voiceChangeBefore(messages, 1)).toBeNull()
    expect(voiceChangeBefore(messages, 0)).toBeNull()
  })

  it('labels the first speaker and later speaker changes', () => {
    const messages = groupTurns([
      turn({ id: 'u', msg_seq: 0, role: 'user', voice_id: null }),
      turn({ id: 'a', msg_seq: 1, voice_id: 'v1' }),
      turn({ id: 'b', msg_seq: 2, voice_id: 'v1' }),
      turn({ id: 'c', msg_seq: 3, voice_id: 'v2' }),
    ])
    expect(speakerBefore(messages, 0)).toEqual({ role: 'user', voiceId: null })
    expect(speakerBefore(messages, 1)).toEqual({ role: 'assistant', voiceId: 'v1' })
    expect(speakerBefore(messages, 2)).toBeNull()
    expect(speakerBefore(messages, 3)).toEqual({ role: 'assistant', voiceId: 'v2' })
  })
})
