import { type ReactNode, useMemo, useState } from 'react'
import {
  type Voice,
  activeReferenceLabel,
  effectiveDurationS,
  isCroppedReference,
  isSampleVoice,
  sourceDurationS,
} from '@/lib/api'
import { formatSeconds } from '@/lib/format'
import { cn } from '@/lib/utils'

/**
 * The one voice list: the generate picker and the voice lab library differ in what
 * picking means (activate versus view), which the caller owns in `onSelect`. The
 * selected row wears `renderExpanded` when the caller supplies one, so the lab
 * library stays compact.
 */
export function VoiceList({
  voices,
  selectedId,
  onSelect,
  note = 'No voices yet.',
  renderExpanded,
}: {
  voices: Voice[]
  selectedId: string | null
  onSelect: (voice: Voice) => void
  note?: string
  renderExpanded?: (voice: Voice) => ReactNode
}) {
  const [query, setQuery] = useState('')
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const filtered =
      needle.length === 0
        ? voices
        : voices.filter(
            (voice) =>
              voice.name.toLowerCase().includes(needle) ||
              (voice.effective_transcript || voice.source_transcript).toLowerCase().includes(needle),
          )
    // The dogfood sample is what the operator reaches for first.
    return [...filtered].sort(
      (left, right) => Number(isSampleVoice(right)) - Number(isSampleVoice(left)),
    )
  }, [voices, query])

  return (
    <>
      <input
        className="w-full rounded-md border border-zinc-600 bg-zinc-950 px-2 py-1 text-sm text-zinc-100"
        placeholder="Search voices"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {voices.length === 0 ? (
        <p className="text-sm text-zinc-400">{note}</p>
      ) : visible.length === 0 ? (
        <p className="text-sm text-zinc-500">No voice matches.</p>
      ) : (
        <ul className="flex flex-col">
          {visible.map((voice) => {
            const quote = voice.effective_transcript || voice.source_transcript
            const selected = selectedId === voice.id
            const expanded = selected ? renderExpanded?.(voice) : undefined
            return (
              <li key={voice.id} aria-label={expanded ? voice.name : undefined}>
                {expanded ?? (
                  <button
                    type="button"
                    aria-current={selected ? 'true' : undefined}
                    className={cn(
                      'w-full rounded-md px-2.5 py-1.5 text-left',
                      selected ? 'bg-zinc-700 text-zinc-50' : 'text-zinc-200 hover:bg-zinc-800',
                    )}
                    onClick={() => onSelect(voice)}
                  >
                    <span className="block truncate text-sm font-medium">
                      {voice.name}
                      {isSampleVoice(voice) ? (
                        <span className="ml-2 text-[10px] font-normal uppercase tracking-wide text-emerald-400">
                          sample
                        </span>
                      ) : null}
                    </span>
                    <span className="block text-[11px] text-zinc-400">
                      ▶ {activeReferenceLabel(voice)} · {formatSeconds(effectiveDurationS(voice))}
                      {isCroppedReference(voice) ? (
                        <span className="text-zinc-500">
                          {' '}
                          of {formatSeconds(sourceDurationS(voice))}
                        </span>
                      ) : null}
                    </span>
                    {quote ? (
                      <span className="mt-0.5 block truncate text-[11px] italic text-zinc-500">
                        {quote}
                      </span>
                    ) : null}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}
