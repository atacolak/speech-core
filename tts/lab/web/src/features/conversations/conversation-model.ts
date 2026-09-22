import type { ConversationTurn } from '@/lib/api'

export type ConversationMessage = {
  msgSeq: number
  role: 'user' | 'assistant'
  variations: ConversationTurn[]
}

export function groupTurns(turns: ConversationTurn[]): ConversationMessage[] {
  const slots = new Map<number, ConversationTurn[]>()
  for (const turn of turns) {
    const slot = slots.get(turn.msg_seq) ?? []
    slot.push(turn)
    slots.set(turn.msg_seq, slot)
  }
  return [...slots.entries()]
    .sort(([a], [b]) => a - b)
    .map(([msgSeq, variations]) => ({
      msgSeq,
      role: variations[0].role,
      variations: [...variations].sort((a, b) => a.variation_seq - b.variation_seq),
    }))
}

export function chosenTurn(message: ConversationMessage): ConversationTurn {
  return message.variations.find((turn) => turn.chosen) ?? message.variations[0]
}

export function voiceChangeBefore(
  messages: ConversationMessage[],
  index: number,
): string | null {
  const current = messages[index]
  if (current.role !== 'assistant') return null
  const voice = chosenTurn(current).voice_id
  for (let i = index - 1; i >= 0; i -= 1) {
    if (messages[i].role !== 'assistant') continue
    const previous = chosenTurn(messages[i]).voice_id
    return previous !== voice ? previous : null
  }
  return null
}
