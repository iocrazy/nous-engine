import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './client'
import { useToastStore } from '../stores/toast'

// Unified monitor endpoint (replaces the old Rust sidecar). The legacy
// useGpuStats / GpuSummary on /engines/gpus was unused — DashboardOverlay
// reads the GPU section out of useMonitorStats now, so we removed the
// 3s duplicate poll.

export interface GpuProcessInfo {
  pid: number
  gpu: number
  used_gpu_memory_mb: number
  name: string
  /** Truncated cmdline(前 8 段 / 120 字符)用于行内紧凑显示。 */
  command: string
  /** PR-10:完整 cmdline,前端 hover tooltip 展示 — 路径太长时行内截断后用户能看全。
   * 旧后端 payload 没此字段,渲染时降级用 `command`。 */
  command_full?: string
  managed: boolean
  model_name: string | null
}

export interface SysGpuInfo {
  index: number
  name: string
  utilization_gpu: number
  utilization_memory: number
  temperature: number
  fan_speed: number
  power_draw_w: number
  power_limit_w: number
  memory_used_mb: number
  memory_total_mb: number
  memory_free_mb: number
  processes: GpuProcessInfo[]
  loaded_models?: { name: string; type: string; vram_gb: number }[]
  low_memory?: boolean
}

export interface SysGpuResponse {
  count: number
  gpus: SysGpuInfo[]
}

export interface SystemStats {
  cpu_usage_percent: number
  cpu_count: number
  cpu_per_core: number[]
  memory_total_gb: number
  memory_used_gb: number
  memory_available_gb: number
  swap_total_gb: number
  swap_used_gb: number
  disk_total_gb: number
  disk_used_gb: number
  disk_percent: number
}

export interface ProcessInfo {
  pid: number
  name: string
  cpu_percent: number
  memory_mb: number
  command: string
  // spec 2026-09-06 process-net-traffic:采集器不可用时为 null(不是 0)。
  net_tx_bps: number | null
  net_rx_bps: number | null
}

export interface NetProbeInfo {
  available: boolean
  age_s: number | null
}

interface MonitorStatsResponse {
  gpus: SysGpuResponse
  system: SystemStats
  processes: ProcessInfo[]
  net_probe: NetProbeInfo
  uptime_seconds: number
}

// Single query that fetches everything from the Python backend
export function useMonitorStats() {
  return useQuery({
    // 与 gpuStats.ts 的 MONITOR_QK 同键 ['monitor','stats'] → React Query 去重:
    // 此前本 hook 用 ['monitor-stats'],和 gpuStats 的 ['monitor','stats'] 是两个独立
    // 缓存 → 同一 /monitor/stats 端点被两套各 2s 独立轮询。统一后合并为一次(性能 P0)。
    queryKey: ['monitor', 'stats'],
    queryFn: () => apiFetch<MonitorStatsResponse>('/api/v1/monitor/stats'),
    refetchInterval: 2000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  })
}

// Convenience hooks that derive from the unified query
export function useSysGpus() {
  const q = useMonitorStats()
  return { ...q, data: q.data?.gpus }
}

export function useSysStats() {
  const q = useMonitorStats()
  return { ...q, data: q.data?.system }
}

export function useSysProcesses() {
  const q = useMonitorStats()
  return {
    ...q,
    data: q.data ? { processes: q.data.processes, net_probe: q.data.net_probe } : undefined,
  }
}

export interface UsageSummary {
  today: {
    llm_calls: number
    llm_total_tokens: number
    tts_calls: number
    tts_characters: number
    total_calls: number
  }
  all_time: {
    llm_calls: number
    llm_total_tokens: number
  }
}

export function useUsageSummary() {
  return useQuery({
    queryKey: ['usage-summary'],
    queryFn: () => apiFetch<UsageSummary>('/api/v1/monitor/usage/summary'),
    refetchInterval: 10_000,
  })
}

export interface InferenceUsageRow {
  day?: string
  hour?: string
  model?: string | null
  instance?: number | null
  apikey?: number | null
  input_tokens: number
  output_tokens: number
  req_cnt: number
}

export interface InferenceUsage {
  interval: 'day' | 'hour'
  group_by: string
  start: string
  end: string
  data: InferenceUsageRow[]
}

export function useInferenceUsage(params: {
  interval?: 'day' | 'hour'
  group_by?: 'Model' | 'Instance' | 'ApiKey'
  instance_id?: number
  model?: string
} = {}) {
  const qs = new URLSearchParams()
  if (params.interval) qs.set('interval', params.interval)
  if (params.group_by) qs.set('group_by', params.group_by)
  if (params.instance_id) qs.set('instance_id', String(params.instance_id))
  if (params.model) qs.set('model', params.model)
  const url = `/api/v1/monitor/usage/inference?${qs.toString()}`
  return useQuery({
    queryKey: ['inference-usage', qs.toString()],
    queryFn: () => apiFetch<InferenceUsage>(url),
    refetchInterval: 30_000,
  })
}


export function useKillProcess() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (pid: number) =>
      apiFetch<{ killed: boolean; pid: number }>('/api/v1/monitor/kill-process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pid }),
      }),
    onSuccess: (_, pid) => {
      qc.invalidateQueries({ queryKey: ['monitor-stats'] })
      useToastStore.getState().add(`Process ${pid} killed`, 'success')
    },
    onError: (error: Error) => {
      useToastStore.getState().add(`Kill failed: ${error.message}`, 'error')
    },
  })
}
