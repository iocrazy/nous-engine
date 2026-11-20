/**
 * useGpuStats — 全局 GPU 利用率 polling hook(PR-7,任务面板重置 cook overview)。
 *
 * 后端 `/api/v1/monitor/stats` 已有完整 GPU 数据(走 nvidia-smi)。本 hook 2s 轮询
 * 兜底,task panel cook overview 显示「GPU 1·62%」(对齐 mockup variant-final 上方
 * 信息条 GPU info)。轮询是 fine — GPU util 不需要 sub-second 实时。
 *
 * 多卡 nous 场景:挑「主卡」展示。当前简单策略 = cuda:1(Pro 6000,跑大模型主力)
 * 优先;若 cuda:1 不存在,fallback 第一个 GPU。后续可按「最忙卡」/「running task 所在卡」
 * 动态切换。
 */
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './client'

export interface GpuInfo {
  index: number
  name: string
  utilization_gpu: number
  utilization_memory: number
  temperature: number
  power_draw_w: number
  memory_used_mb: number
  memory_total_mb: number
  memory_free_mb?: number
  power_limit_w?: number
  processes?: {
    pid: number
    name: string
    model_name: string | null
    used_gpu_memory_mb: number
  }[]
}

export interface SystemStats {
  cpu_usage_percent: number
  cpu_count: number
  memory_total_gb: number
  memory_used_gb: number
  memory_available_gb: number
}

interface MonitorStats {
  gpus: { count: number; gpus: GpuInfo[] }
  system: SystemStats
}

const fetchMonitorStats = () => apiFetch<MonitorStats>('/api/v1/monitor/stats')
const MONITOR_QK = ['monitor', 'stats'] as const

// useGpuStats / useSystemStats 共享同一 queryKey → React Query 去重,一次轮询两处用。
export function useGpuStats() {
  return useQuery({
    queryKey: MONITOR_QK,
    queryFn: fetchMonitorStats,
    select: (d) => d.gpus.gpus,
    refetchInterval: 2_000,
    staleTime: 1_500,
  })
}

export function useSystemStats() {
  return useQuery({
    queryKey: MONITOR_QK,
    queryFn: fetchMonitorStats,
    select: (d) => d.system,
    refetchInterval: 2_000,
    staleTime: 1_500,
  })
}

/**
 * 挑「主卡」展示策略:
 * - 优先 cuda:1(nous 多卡部署里 Pro 6000 = 主跑卡)
 * - fallback 第一个 GPU
 */
export function pickPrimaryGpu(gpus: GpuInfo[] | undefined): GpuInfo | null {
  if (!gpus || gpus.length === 0) return null
  return gpus.find((g) => g.index === 1) ?? gpus[0]
}
