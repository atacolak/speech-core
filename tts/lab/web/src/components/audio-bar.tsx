export function AudioBar({
  src,
  label,
}: {
  src: string | undefined
  label: string
}) {
  if (!src) {
    return null
  }
  return (
    <div className="flex flex-col gap-1">
      {label ? (
        <p className="text-[11px] font-medium uppercase tracking-wide text-zinc-400">{label}</p>
      ) : null}
      <audio key={src} className="h-8 w-full" controls preload="metadata" src={src} />
    </div>
  )
}
