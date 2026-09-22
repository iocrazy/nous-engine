import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './client'

export type VLLMConfig = {
  block_size?: string
  cache_dtype?: string
  enable_prefix_caching?: string
  gpu_memory_utilization?: string
  num_gpu_blocks?: string
}

export type VLLMStats = {
  running?: number
  waiting?: number
  kv_cache_usage_perc?: number
  prefix_cache_queries_total?: number
  prefix_cache_hits_total?: number
  prefix_cache_hit_rate?: number
  num_preemptions_total?: number
}

export type VLLMInstance = {
  name: string
  port: number
  healthy: boolean
  config: VLLMConfig
  stats: VLLMStats
  error: string | null
}

export type VLLMSnapshot = { instances: VLLMInstance[] }

export function useVLLMMetrics(opts?: { refetchInterval?: number }) {
  return useQuery({
    queryKey: ['observability', 'vllm'],
    queryFn: () => apiFetch<VLLMSnapshot>('/api/v1/observability/vllm'),
    // Default 3s — KV usage moves fast under load. Caller can override.
    refetchInterval: opts?.refetchInterval ?? 3_000,
    staleTime: 0,
    retry: false,
  })
}

// 与后端 _LAUNCH_PARAM_WHITELIST 一一对应。
// **刻意不含 gpu_memory_utilization / tensor_parallel_size** —— 后端会 400:
//   显存走 vram-budget(绝对 GiB,加载时按实际那张卡换算);tp 由放置决定。
// null = 清除该覆盖,回退 models.d 的 yaml 值。
export type LaunchParamsBody = {
  max_model_len?: number | null
  max_num_seqs?: number | null
  max_num_batched_tokens?: number | null
  enable_prefix_caching?: boolean | null
  dtype?: string | null
  quantization?: string | null
}

export function useUpdateLaunchParams() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (params: { name: string; body: LaunchParamsBody }) =>
      apiFetch<{ name: string; params: object; applied: boolean; hint: string }>(
        `/api/v1/engines/${encodeURIComponent(params.name)}/launch-params`,
        { method: 'PATCH', body: JSON.stringify(params.body) },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['engines'] })
    },
  })
}
