export type RuntimeInfo = {
  selected: string
  status: string
  leftover_parked: boolean
  not_a_pin_swap: boolean
  voicecat_path: boolean
}

export async function fetchRuntime(): Promise<RuntimeInfo> {
  const response = await fetch('/api/runtime')
  if (!response.ok) {
    throw new Error(`runtime ${response.status}`)
  }
  return (await response.json()) as RuntimeInfo
}
