import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { TakePlayer } from '@/components/take-player'
import {
  chosenTurn,
  groupTurns,
  speakerBefore,
} from '@/features/conversations/conversation-model'
import {
  artifactAudioUrl,
  chooseTurnVariation,
  deleteConversation,
  deleteTurnVariation,
  fetchConversation,
  fetchConversations,
  fetchRuntime,
  fetchVoices,
  formatApiError,
  putConversationRingLimit,
  regenerateTurn,
  saveConversation,
  saveTurnVariation,
  type ConversationTurn,
  type Voice,
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

function voiceName(voices: Voice[], voiceId: string | null): string {
  if (!voiceId) return 'You'
  return voices.find((voice) => voice.id === voiceId)?.name ?? voiceId
}

function firstUserText(turns: ConversationTurn[]): string | null {
  const text = turns.find((turn) => turn.role === 'user')?.text.trim()
  return text || null
}

function railTitle(
  item: { id: string; title: string | null },
  details: Array<{ id: string; turns: ConversationTurn[] } | undefined>,
): string {
  const stored = item.title?.trim()
  if (stored) return stored
  const detail = details.find((row) => row?.id === item.id)
  return firstUserText(detail?.turns ?? []) || item.id
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
    <article
      aria-label={`${turn.role} turn`}
      className="flex w-[42%] min-w-64 max-w-lg flex-col gap-1.5 rounded-2xl border border-zinc-700/80 bg-zinc-900/70 px-3 py-2.5"
    >
      {artifactId && timeline ? (
        <TakePlayer
          timeline={timeline}
          autoplay={false}
          live={false}
          onPlayheadChange={setPlayheadS}
          onPlayingChange={setPlaying}
        />
      ) : null}
      <p className="whitespace-pre-wrap break-words text-base italic leading-6 text-zinc-400">
        {spokenText(turn.text, highlight)}
      </p>
    </article>
  )
}

export function ConversationsPane() {
  const client = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [ringLimit, setRingLimit] = useState(3)
  const [regenErrors, setRegenErrors] = useState<Record<string, string>>({})
  const conversations = useQuery({ queryKey: ['conversations'], queryFn: fetchConversations })
  const voices = useQuery({ queryKey: ['voices'], queryFn: fetchVoices })
  const untitledItems = useMemo(
    () => (conversations.data ?? []).filter((item) => !item.title?.trim()),
    [conversations.data],
  )
  const untitledDetails = useQueries({
    queries: untitledItems.map((item) => ({
      queryKey: ['conversation', item.id] as const,
      queryFn: () => fetchConversation(item.id),
    })),
  })
  const untitledRows = untitledDetails.map((query) => query.data)
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
  useEffect(() => {
    if (selectedId === null && items[0]) {
      setSelectedId(items[0].id)
    }
  }, [items, selectedId])
  const messages = detail.data ? groupTurns(detail.data.turns) : []
  const sessionTitle =
    detail.data?.title?.trim() ||
    firstUserText(detail.data?.turns ?? []) ||
    selectedId ||
    'conversation'
  const participantNames = useMemo(() => {
    const names = new Set<string>()
    for (const message of messages) {
      if (message.role !== 'assistant') continue
      names.add(voiceName(voices.data ?? [], chosenTurn(message).voice_id))
    }
    return [...names]
  }, [messages, voices.data])

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
  const removeVariation = useMutation({
    mutationFn: ({ conversationId, turnId }: { conversationId: string; turnId: string }) =>
      deleteTurnVariation(conversationId, turnId),
    onSuccess: (_data, vars) => invalidateConversation(vars.conversationId),
    onError: (error) => toast.error(formatApiError(error)),
  })
  const removeSession = useMutation({
    mutationFn: (conversationId: string) => deleteConversation(conversationId),
    onSuccess: (_data, conversationId) => {
      setSelectedId((current) => (current === conversationId ? null : current))
      void client.invalidateQueries({ queryKey: ['conversations'] })
    },
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
          {selectedId ? (
            <button
              type="button"
              className="rounded-md px-2 py-1 text-sm text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
              onClick={() => removeSession.mutate(selectedId)}
            >
              Delete conversation
            </button>
          ) : null}
        </div>
        {detail.data ? (
          <p className="mt-2 flex min-w-0 items-baseline gap-2 text-sm">
            <span className="truncate font-semibold text-zinc-50">{sessionTitle}</span>
            {participantNames.length > 0 ? (
              <>
                <span className="text-zinc-500">·</span>
                <span className="truncate text-zinc-400">{participantNames.join(', ')}</span>
              </>
            ) : null}
          </p>
        ) : null}
      </header>
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <aside className="flex w-72 shrink-0 flex-col gap-2 overflow-y-auto border-r border-zinc-700 p-3">
          {items.length === 0 ? (
            <p className="px-1 text-sm text-zinc-400">
              no conversations yet. a live desk call opens a session.
            </p>
          ) : null}
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
              <span className="block truncate">{railTitle(item, untitledRows)}</span>
              {item.saved ? (
                <span className="text-[11px] uppercase tracking-wide text-emerald-400">saved</span>
              ) : null}
            </button>
          ))}
        </aside>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
          {detail.data && messages.length === 0 ? (
            <p className="text-sm text-zinc-400">
              no turns in this session. live-call opened it; leftover mouth pcm
              and leftover-accepted transcript never landed.
            </p>
          ) : null}
          {messages.map((message, index) => {
            const turn = chosenTurn(message)
            const speaker = speakerBefore(messages, index)
            const mine = message.role === 'user'
            return (
              <div className={cn('flex flex-col gap-1', mine ? 'items-start' : 'items-end')} key={message.msgSeq}>
                {speaker ? (
                  <p className="text-[11px] font-normal text-zinc-500" data-testid="speaker-label">
                    {speaker.role === 'user' ? 'You' : voiceName(voices.data ?? [], speaker.voiceId)}
                  </p>
                ) : null}
                <TurnView turn={turn} />
                <div className={cn('flex flex-wrap items-center gap-2', mine ? '' : 'flex-row-reverse')}>
                  {message.variations.length > 1 ? (
                    <label className="flex items-center gap-1 text-[11px] text-zinc-400">
                      <span className="sr-only">variation</span>
                      <select
                        aria-label="variation"
                        className="rounded-md border border-zinc-600 bg-zinc-950 px-1.5 py-0.5 text-xs text-zinc-200"
                        value={turn.id}
                        onChange={(event) => {
                          if (!selectedId) return
                          chooseVariation.mutate({
                            conversationId: selectedId,
                            turnId: event.target.value,
                          })
                        }}
                      >
                        {message.variations.map((variation) => (
                          <option key={variation.id} value={variation.id}>
                            {`V${variation.variation_seq + 1}`}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : null}
                  {message.role === 'assistant' ? (
                    <button
                      type="button"
                      disabled={liveCallActive}
                      className="rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-40"
                      onClick={() => {
                        if (!selectedId) return
                        regenerate.mutate({ conversationId: selectedId, turnId: turn.id })
                      }}
                    >
                      Regenerate
                    </button>
                  ) : null}
                  {turn.audio_artifact_id ? (
                    <button
                      type="button"
                      className="rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
                      onClick={() => {
                        if (!selectedId) return
                        saveVariation.mutate({ conversationId: selectedId, turnId: turn.id })
                      }}
                    >
                      Save to voice
                    </button>
                  ) : null}
                  {message.variations.length > 1 ? (
                    <button
                      type="button"
                      className="rounded-md px-2 py-1 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
                      onClick={() => {
                        if (!selectedId) return
                        removeVariation.mutate({ conversationId: selectedId, turnId: turn.id })
                      }}
                    >
                      Delete variation
                    </button>
                  ) : null}
                  {regenErrors[turn.id] ? (
                    <p className="text-xs text-red-400">{regenErrors[turn.id]}</p>
                  ) : null}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
