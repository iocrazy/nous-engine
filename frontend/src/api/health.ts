import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './client'

export interface HealthStartup {
  resident_total: number
  resident_loaded: number
  preloading: boolean
}

/** ComfyUI sidecar 体检(后端 comfy/sidecar_status.py)。旧后端没有这个字段。 */
export interface HealthComfy {
  state: 'ok' | 'degraded' | 'down'
  online: boolean
  identity: 'systemd' | 'foreign' | 'none' | 'unknown'
  listens: string[]
  missing_listens: string[]
  problems: string[]
}

export interface HealthRunner {
  group_id: string
  running?: boolean
  healthy?: boolean
}

export interface Health {
  status: string
  database?: string
  gpus?: number
  models_loaded?: number
  load_failures?: Record<string, string>
  startup?: HealthStartup
  runners?: HealthRunner[]
  comfy?: HealthComfy
}

/** /health 为什么是 degraded —— 与后端判 degraded 的四处一一对应,给顶栏 title 用。 */
export function healthDegradedReasons(h: Health): string[] {
  const out: string[] = []
  if (h.database && h.database !== 'ok') out.push(`database ${h.database}`)
  const failed = Object.keys(h.load_failures ?? {})
  if (failed.length) out.push(`模型加载失败:${failed.join(', ')}`)
  for (const r of h.runners ?? []) {
    if (!(r.healthy ?? r.running ?? false)) out.push(`runner ${r.group_id} 不健康`)
  }
  if (h.comfy && h.comfy.state !== 'ok') {
    out.push(...(h.comfy.problems.length ? h.comfy.problems : [`ComfyUI ${h.comfy.state}`]))
  }
  return out
}

/** 轮询 /health(unauth)。预加载中 3s 一次(给「模型加载中」横幅及时更新),
 *  否则 20s 一次(低开销)。 */
export function useHealth() {
  return useQuery<Health>({
    queryKey: ['health'],
    queryFn: () => apiFetch('/health'),
    refetchInterval: (q) => (q.state.data?.startup?.preloading ? 3000 : 20000),
  })
}
