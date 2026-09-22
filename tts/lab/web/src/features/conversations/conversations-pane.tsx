import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { TakePlayer } from '@/components/take-player'
import { chosenTurn, groupTurns, voiceChangeBefore } from '@/features/conversations/conversation-model'
import {
  artifactAudioUrl,
  chooseTurnVariation,
  fetchConversation,
  fetchConversations,
  fetchRuntime,
  formatApiError,
  putConversationRingLimit,
  regenerateTurn,
  saveConversation,
  saveTurnVariation,
  type ConversationTurn,
} from '@/lib/api'
import { decodePcmWav, type PcmTimeline } from '@/lib/pcm-timeline'
import { highlightAt, type Highlight } from '@/lib/spoken-alignment'
import { cn } from '@/lib/utils'

function spokenText(text: string, highlight: Highlight | null): ReactNode[] {
  const nodes: ReactNode[] = []
  let wordIndex = 0
  for (const part of text.split(/(\s+)/)) {
    if (part.length === 0) {
      continue
    }
    if (/^\s+$/u.test(part)) {
      nodes.push(part)
      continue
    }
    nodes.push(
      wordIndex === highlight?.wordIndex ? (
        <mark
          className="rounded-sm bg-[rgba(56,189,248,0.18)] shadow-[0_0_0_1px_rgba(56,189,248,0.85)]"
          key={wordIndex}
          title={highlight.title}
        >
          {part}
        </mark>
      ) : (
        part
      ),
    )
    wordIndex += 1
  }
  return nodes
}

function TurnView({ turn }: { turn: ConversationTurn }) {
  const [timeline, setTimeline] = useState<PcmTimeline | null>(null)
  const [playing, setPlaying] = useState(false)
  const [playheadS, setPlayheadS] = useState(0)
  const artifactId = turn.audio_artifact_id

  useEffect(() => {
    if (!artifactId) {
      setTimeline(null)
      return
    }
    let cancelled = false
    fetch(artifactAudioUrl(artifactId))
      .then((response) => (response.ok ? response.arrayBuffer() : null))
      .then((bytes) => {
        const decoded = bytes === null ? null : decodePcmWav(bytes)
        if (cancelled || decoded === null) {
          return
        }
        setTimeline(decoded)
      })
      .catch(() => {
        // A turn that will not decode simply leaves the player empty.
      })
    return () => {
      cancelled = true
    }
  }, [artifactId])

  const highlight = playing ? highlightAt(playheadS, turn.text, turn.alignment, null) : null

  return (
    <article aria-label={`${turn.role} turn`} className="flex flex-col gap-2">
      <p className="whitespace-pre-wrap break-words text-sm text-zinc-100">
        {spokenText(turn.text, highlight)}
      </p>
      {artifactId && timeline ? (
        <TakePlayer
          timeline={timeline}
          autoplay={false}
          live={false}
          onPlayheadChange={setPlayheadS}
          onPlayingChange={setPlaying}
        />
      ) : null}
    </article>
  )
}

export function ConversationsPane() {
  const client = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [ringLimit, setRingLimit] = useState(3)
  const [regenErrors, setRegenErrors] = useState<Record<string, string>>({})
  const conversations = useQuery({ queryKey: ['conversations'], queryFn: fetchConversations })
  const detail = useQuery({
    queryKey: ['conversation', selectedId],
    queryFn: () => fetchConversation(selectedId!),
    enabled: Boolean(selectedId),
  })
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: fetchRuntime })
  const liveCallActive = Boolean(runtime.data?.live_call_active)
  const items = useMemo(
    () =>
      [...(conversations.data ?? [])].sort((left, right) =>
        right.started_at.localeCompare(left.started_at),
      ),
    [conversations.data],
  )
  const messages = detail.data ? groupTurns(detail.data.turns) : []

  const invalidateConversation = (conversationId: string) => {
    void client.invalidateQueries({ queryKey: ['conversations'] })
    void client.invalidateQueries({ queryKey: ['conversation', conversationId] })
  }

  const chooseVariation = useMutation({
    mutationFn: ({ conversationId, turnId }: { conversationId: string; turnId: string }) =>
      chooseTurnVariation(conversationId, turnId),
    onSuccess: (_data, vars) => invalidateConversation(vars.conversationId),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const regenerate = useMutation({
    mutationFn: ({ conversationId, turnId }: { conversationId: string; turnId: string }) =>
      regenerateTurn(conversationId, turnId),
    onSuccess: (_data, vars) => {
      setRegenErrors((current) => {
        const next = { ...current }
        delete next[vars.turnId]
        return next
      })
      invalidateConversation(vars.conversationId)
    },
    onError: (error, vars) => {
      const message = formatApiError(error)
      toast.error(message)
      setRegenErrors((current) => ({ ...current, [vars.turnId]: message }))
    },
  })
  const saveVariation = useMutation({
    mutationFn: ({ conversationId, turnId }: { conversationId: string; turnId: string }) =>
      saveTurnVariation(conversationId, turnId),
    onSuccess: (_data, vars) => invalidateConversation(vars.conversationId),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const saveSession = useMutation({
    mutationFn: (conversationId: string) => saveConversation(conversationId),
    onSuccess: (_data, conversationId) => invalidateConversation(conversationId),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const updateRing = useMutation({
    mutationFn: (limit: number) => putConversationRingLimit(limit),
    onSuccess: (body) => {
      setRingLimit(body.ring_limit)
      void client.invalidateQueries({ queryKey: ['conversations'] })
    },
    onError: (error) => toast.error(formatApiError(error)),
  })

  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <header className="border-b border-zinc-700 px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-sm font-semibold text-zinc-50">CONVERSATIONS</h2>
          <label className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-zinc-400">
            Ring limit
            <input
              type="number"
              min={1}
              max={50}
              className="w-16 rounded-md border border-zinc-600 bg-zinc-950 px-1.5 py-1 text-right text-sm normal-case text-zinc-100"
              value={ringLimit}
              onChange={(event) => {
                const next = Number(event.target.value)
                if (!Number.isNaN(next)) {
                  updateRing.mutate(next)
                }
              }}
            />
          </label>
          {detail.data && !detail.data.saved && selectedId ? (
            <button
              type="button"
              className="rounded-md px-2 py-1 text-sm text-zinc-200 hover:bg-zinc-800"
              onClick={() => saveSession.mutate(selectedId)}
            >
              Save
            </button>
          ) : null}
        </div>
      </header>
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <aside className="flex w-72 shrink-0 flex-col gap-2 overflow-y-auto border-r border-zinc-700 p-3">
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-current={selectedId === item.id ? 'true' : undefined}
              className={cn(
                'rounded-md px-2.5 py-1.5 text-left text-sm',
                selectedId === item.id
                  ? 'bg-zinc-700 font-medium text-zinc-50'
                  : 'text-zinc-200 hover:bg-zinc-800',
              )}
              onClick={() => setSelectedId(item.id)}
            >
              <span className="block truncate">{item.id}</span>
              {item.saved ? (
                <span className="text-[11px] uppercase tracking-wide text-emerald-400">saved</span>
              ) : null}
            </button>
          ))}
        </aside>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
          {messages.map((message, index) => {
            const turn = chosenTurn(message)
            const changedFrom = voiceChangeBefore(messages, index)
            return (
              <div className="flex flex-col gap-2" key={message.msgSeq}>
                {changedFrom ? (
                  <p className="text-[11px] uppercase tracking-wide text-zinc-500">
                    voice changed from {changedFrom}
                  </p>
                ) : null}
                {message.variations.length > 1 ? (
                  <div className="flex flex-wrap gap-1">
                    {message.variations.map((variation) => (
                      <button
                        key={variation.id}
                        type="button"
                        aria-current={turn.id === variation.id ? 'true' : undefined}
                        className={cn(
                          'rounded-md px-2 py-0.5 text-xs',
                          turn.id === variation.id
                            ? 'bg-zinc-700 font-medium text-zinc-50'
                            : 'text-zinc-300 hover:bg-zinc-800',
                        )}
                        onClick={() => {
                          if (!selectedId) return
                          chooseVariation.mutate({ conversationId: selectedId, turnId: variation.id })
                        }}
                      >
                        {`V${variation.variation_seq + 1}`}
                      </button>
                    ))}
                  </div>
                ) : null}
                <TurnView turn={turn} />
                {message.role === 'assistant' ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      disabled={liveCallActive}
                      className="rounded-md px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
                      onClick={() => {
                        if (!selectedId) return
                        regenerate.mutate({ conversationId: selectedId, turnId: turn.id })
                      }}
                    >
                      Regenerate
                    </button>
                    {turn.audio_artifact_id ? (
                      <button
                        type="button"
                        className="rounded-md px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
                        onClick={() => {
                          if (!selectedId) return
                          saveVariation.mutate({ conversationId: selectedId, turnId: turn.id })
                        }}
                      >
                        Save to voice
                      </button>
                    ) : null}
                    {regenErrors[turn.id] ? (
                      <p className="text-xs text-red-400">{regenErrors[turn.id]}</p>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
