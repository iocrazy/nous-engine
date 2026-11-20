import { useInfiniteQuery, useQuery, useMutation, useQueryClient, type InfiniteData } from '@tanstack/react-query'
import { apiFetch } from './client'
import { useLiveChannel } from './useLiveChannel'
import { useTaskProgressStore } from '../stores/taskProgress'

export interface ExecutionTask {
  id: string
  workflow_id: string | null
  workflow_name: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  nodes_total: number
  nodes_done: number
  current_node: string | null
  result: any
  error: string | null
  duration_ms: number | null
  /** 历史调用入参(flat {exposed_key: value})。「重跑(相同参数)」回填用。 */
  input_json?: Record<string, unknown> | null
  created_at: string
  updated_at: string
  /** Server-derived from result envelope. null until the run completes
   * with an image_output, then 'image'. Used for the task card badge. */
  task_type: 'image' | 'tts' | 'llm' | 'vision' | 'asr' | 'video' | null
  image_width: number | null
  image_height: number | null

  // PR-1a/1b/1c/1d 后端落 task_type 检测时同步暴露的字段;旧 payload 缺 → undefined。
  /** PR-1a/b/c/d:显式 ServiceType,优先于 task_type 读(getTaskType helper)。
   * 'video' 目前后端不产(execution_task_serialize.py 只探前五种),先在类型里
   * 留位:comfy bridge 的视频任务眼下靠 getTaskType 的 workflow_name 兜底认出来,
   * 等后端补 _detect_video_meta 后这条直读路径即可生效。 */
  type?: 'image' | 'tts' | 'llm' | 'vision' | 'asr' | 'video' | null
  /** PR-1b:TTS 任务音频时长(秒)。 */
  audio_duration_seconds?: number | null
  /** PR-1c:LLM 任务 token 统计。 */
  llm_prompt_tokens?: number | null
  llm_completion_tokens?: number | null
  /** PR-1d:Vision (多模态 LLM) 任务 completion tokens。 */
  vision_completion_tokens?: number | null
  /** Arc 3:ASR(直连转写)任务 —— 音频时长(秒)+ 说话人分段数,卡片摘要用。 */
  audio_seconds?: number | null
  segments_count?: number | null

  // —— V1.5 新增（Lane I，对齐 Lane B execution_tasks schema）——
  // 全部 optional：旧后端 payload 不带这些字段时为 undefined，
  // 组件按 undefined 降级（缩略图 → ImageIcon，runner 标识 → 不显示）。
  /** 落到哪个 hardware.yaml group（"image" / "llm-tp" / "tts"）。 */
  gpu_group?: string | null
  /** 实际执行的 runner 实例 id。 */
  runner_id?: string | null
  /** queued 态时的排队序号（1-based）；非 queued 态为 null/undefined。 */
  queue_position?: number | null
  /** image 任务的输出缩略图 URL 列表（数据源 outputs/{task_id}/，
   * 后端 Lane D 落盘 + /tasks 序列化时带出）。空 → 降级 ImageIcon。 */
  output_thumbnails?: string[] | null
}

/** Fetch tasks once, then rely on WebSocket for updates. */
export function useTasks() {
  const qc = useQueryClient()
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = `${protocol}//${window.location.host}/ws/tasks`

  useLiveChannel(url, {
    onMessage: (data) => {
      // PR-6:WS 单一通道复用 — task list 更新 + L3 progress event 同走 /ws/tasks。
      // 按 event 字段路由:`progress` 进 taskProgressStore;其它(created/updated/deleted)
      // 触发 react-query invalidate 重拉 task list。
      if (data && typeof data === 'object' && (data as Record<string, unknown>).event === 'progress') {
        const ev = data as Record<string, unknown>
        const taskId = String(ev.task_id ?? '')
        if (!taskId) return
        useTaskProgressStore.getState().setProgress(taskId, {
          stage: typeof ev.stage === 'string' ? ev.stage : null,
          step: typeof ev.step === 'number' ? ev.step : null,
          totalSteps: typeof ev.total_steps === 'number' ? ev.total_steps : null,
          stepLatencyMs: typeof ev.step_latency_ms === 'number' ? ev.step_latency_ms : null,
          etaMs: typeof ev.eta_ms === 'number' ? ev.eta_ms : null,
          progress: typeof ev.progress === 'number' ? ev.progress : null,
        })
        return
      }
      // 默认 task list 翻新 + 终态任务清掉 progress entry
      qc.invalidateQueries({ queryKey: ['tasks'] })
      if (data && typeof data === 'object') {
        const ev = data as Record<string, unknown>
        const task = ev.task as Record<string, unknown> | undefined
        const status = typeof task?.status === 'string' ? task.status : null
        const taskId = typeof task?.id === 'string' ? task.id : null
        if (taskId && status && status !== 'running' && status !== 'queued') {
          useTaskProgressStore.getState().clear(taskId)
        }
      }
    },
    onReconnect: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })

  return useQuery({
    queryKey: ['tasks'],
    queryFn: () => apiFetch<ExecutionTask[]>('/api/v1/tasks?limit=50&include_input=false'),
    // WebSocket handles real-time updates; this is a safety net for the
    // half-open-socket case the browser may not surface.
    refetchInterval: 60_000,
  })
}

export interface ServiceUsageTask extends ExecutionTask {
  comfy_usage: {
    duration_ms: number | null
    llm_nodes: { local_qwen: number; deepseek: number } | null
  } | null
}

export function useServiceUsageTasks(serviceId: string) {
  return useQuery({
    queryKey: ['services', serviceId, 'usage'],
    queryFn: () => apiFetch<ServiceUsageTask[]>(`/api/v1/services/${encodeURIComponent(serviceId)}/usage`),
    refetchInterval: 10_000,
  })
}

export type GallerySort = 'newest' | 'oldest' | 'model'
export type GalleryMedia = 'all' | 'image' | 'video'

export type GalleryTask = Pick<ExecutionTask, 'id' | 'workflow_id' | 'workflow_name' | 'created_at' | 'duration_ms'> & {
  output_thumbnails: string[]
  video_url: string | null
  kind: Exclude<GalleryMedia, 'all'>
  expires_at: string
  expired: boolean
  model: string
  parameters: Record<string, string>
}

type GalleryPage = { items: GalleryTask[]; next_before: string | null; next_model: string | null }
type GalleryCursor = { id: string; model: string | null }

export function useGalleryModels() {
  return useQuery({
    queryKey: ['tasks', 'gallery-models'],
    queryFn: ({ signal }) => apiFetch<string[]>('/api/v1/tasks/gallery/models', { signal }),
    staleTime: 60_000,
  })
}

export function useImageTasks(media: GalleryMedia, query: string, days: number | null,
  model: string, sort: GallerySort, tag: string,
) {
  return useInfiniteQuery({
    queryKey: ['tasks', 'gallery', media, query, days, model, sort, tag],
    queryFn: ({ signal, pageParam }) => {
      const params = new URLSearchParams({ media, sort })
      if (pageParam) {
        params.set('before', pageParam.id)
        if (pageParam.model) params.set('before_model', pageParam.model)
      }
      if (query) params.set('q', query)
      if (days) params.set('days', String(days))
      if (model) params.set('model', model)
      if (tag) params.set('tag', tag)
      return apiFetch<GalleryPage>(`/api/v1/tasks/gallery?${params}`, { signal })
    },
    initialPageParam: null as GalleryCursor | null,
    getNextPageParam: page => page.next_before ? { id: page.next_before, model: page.next_model } : null,
    staleTime: 10_000,
  })
}

export function getTask(id: string, signal?: AbortSignal) {
  return apiFetch<ExecutionTask>(`/api/v1/tasks/${encodeURIComponent(id)}`, { signal })
}

export function useTaskDetail(id: string | null) {
  return useQuery({
    queryKey: ['tasks', 'detail', id],
    queryFn: ({ signal }) => getTask(id!, signal),
    enabled: !!id,
  })
}

export function useCancelTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/tasks/${id}/cancel`, { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}

export function useRetryTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/tasks/${id}/retry`, { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}

export function useDeleteTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/tasks/${id}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}


type GalleryDeletion = {
  deleted_ids: string[]
  skipped: number
  removed_files: number
  missing_files: number
  shared_files: number
}

export function useDeleteGalleryTasks() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { task_ids: string[]; permanent: boolean }) =>
      apiFetch<GalleryDeletion>('/api/v1/tasks/gallery/delete', {
        method: 'POST',
        body: JSON.stringify(input),
      }),
    onSuccess: result => {
      const deleted = new Set(result.deleted_ids)
      qc.setQueriesData<InfiniteData<GalleryPage>>({ queryKey: ['tasks', 'gallery'] }, previous => previous && ({
        ...previous,
        pages: previous.pages.map(page => ({ ...page, items: page.items.filter(item => !deleted.has(item.id)) })),
      }))
      void qc.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}
