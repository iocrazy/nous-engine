import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './client'

export type ComponentStatus = 'operational' | 'idle' | 'degraded' | 'down'
export type DayStatus = ComponentStatus | 'nodata'

export interface StatusDay {
  date: string
  uptime_pct: number | null
  status: DayStatus
  samples: number
}

export interface StatusComponent {
  key: string
  name: string
  status: ComponentStatus
  uptime_7d: number | null
  days: StatusDay[]
  /** 一句话原因(目前只有 comfy:sidecar 体检的 problems,「;」连接)。旧后端没有。 */
  detail?: string | null
}

export interface StatusSnapshot {
  overall: ComponentStatus
  updated_at: string
  components: StatusComponent[]
}

export function useStatus() {
  return useQuery<StatusSnapshot>({
    queryKey: ['status-snapshot'],
    queryFn: () => apiFetch('/api/v1/status'),
    refetchInterval: 15_000, // 状态页 15s 自动刷新
  })
}
