import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { apiFetch } from './client'

// ---------- shared types ----------

export type ServiceStatus = 'active' | 'paused' | 'deprecated' | 'retired'
export type ServiceCategory = 'llm' | 'tts' | 'vl' | 'app' | 'image' | 'asr' | 'embedding'

export interface ExposedParam {
  key?: string
  input_name?: string
  node_id: string
  label?: string
  type?: string
  required?: boolean
  default?: unknown
  constraints?: Record<string, unknown>
  // legacy aliases (from rows backfilled from workflow_apps)
  api_name?: string
  param_key?: string
}

/** 静态枚举:该服务工作流依赖的一个模型/组件。加载状态前端实时叠加
 *  (component 按 file 匹配组件状态;engine 按 engine_key 匹配 /v1/engines)。 */
export interface ServiceModelRef {
  kind: 'component' | 'engine'
  role: string | null
  label: string
  file: string | null
  engine_key: string | null
}

/** model 服务的能力(后端 services/model_capabilities.py 从模型配置推导)。推不出的项为 null/false。 */
export interface ServiceCapabilities {
  /** 上下文窗口(tokens)= max_model_len;null = 引擎自动定 */
  context: number | null
  /** null = 无单独上限,vLLM 默认「上下文 − 输入」 */
  max_output: number | null
  tools: boolean
  /** 思考默认开启,思考内容在 reasoning_content 字段 */
  thinking: boolean
  vision: boolean
  /** source 的 org 段 */
  provider: string | null
  /** 上游仓库 org/name */
  source: string | null
}

export interface ServiceRow {
  id: string
  name: string
  type: string
  status: ServiceStatus
  source_type: 'workflow' | 'preset' | 'model' | 'comfy_template'
  source_id: string | null
  source_name: string | null
  category: ServiceCategory | null
  meter_dim: string | null
  workflow_id: string | null
  workflow_name: string | null  // 来自 backend LEFT JOIN workflows.name
  snapshot_hash: string | null
  snapshot_schema_version: number
  version: number
  /** 开机启动:开机会预加载本服务引用的模型。默认 false —— 没开的服务开机不占显存。 */
  autostart: boolean
  models: ServiceModelRef[]
  /** 只有 model 服务、且在列表端点里才有 */
  capabilities?: ServiceCapabilities | null
  created_at: string
  updated_at: string
}

export interface ServiceDetail extends ServiceRow {
  workflow_snapshot: Record<string, unknown>
  exposed_inputs: ExposedParam[]
  exposed_outputs: ExposedParam[]
}

export interface QuickProvisionBody {
  name: string
  category: ServiceCategory
  engine: string
  label?: string
  params?: Record<string, unknown>
}

export interface PublishBody {
  name: string
  label?: string
  category?: ServiceCategory
  meter_dim?: string
  exposed_inputs: ExposedParam[]
  exposed_outputs: ExposedParam[]
}

// ---------- queries ----------

export function useServices(params?: { category?: ServiceCategory; status?: ServiceStatus }) {
  const qs = new URLSearchParams()
  if (params?.category) qs.set('category', params.category)
  if (params?.status) qs.set('status', params.status)
  const q = qs.toString()
  return useQuery<ServiceRow[]>({
    queryKey: ['services', params?.category ?? null, params?.status ?? null],
    queryFn: () => apiFetch<ServiceRow[]>(`/api/v1/services${q ? `?${q}` : ''}`),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

export function useService(serviceId: string | number | null) {
  return useQuery<ServiceDetail>({
    queryKey: ['service', String(serviceId)],
    queryFn: () => apiFetch<ServiceDetail>(`/api/v1/services/${serviceId}`),
    enabled: serviceId != null,
  })
}

// ---------- mutations ----------

export function useQuickProvision() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: QuickProvisionBody) =>
      apiFetch<ServiceDetail>('/api/v1/services/quick-provision', {
        method: 'POST',
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['services'] })
    },
  })
}

export function usePublishWorkflow() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ workflowId, body }: { workflowId: string | number; body: PublishBody }) =>
      apiFetch<ServiceDetail>(`/api/v1/workflows/${workflowId}/publish`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['services'] })
      // Workflow row + list also flip to status=published; without these
      // the editor topbar still reads the cached status='draft' and
      // shows "发布服务" instead of "取消发布".
      qc.invalidateQueries({ queryKey: ['workflows'] })
      qc.invalidateQueries({ queryKey: ['workflow', String(vars.workflowId)] })
    },
  })
}

export interface PatchServiceBody {
  status?: ServiceStatus
  // 改名(= 改 model 路由键)。⚠️ 用旧 model 名调用的客户端会失效;格式 ^[a-z][a-z0-9-]{1,62}$。
  name?: string
  // 服务页「应用编辑」tab 就地改对外暴露 schema(逐 widget 表单配置)。
  // 省略 = 不动该字段;传 [] = 清空。
  exposed_inputs?: ExposedParam[]
  exposed_outputs?: ExposedParam[]
}

export function usePatchService() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      serviceId,
      ...body
    }: { serviceId: string | number } & PatchServiceBody) =>
      apiFetch<ServiceRow>(`/api/v1/services/${serviceId}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      }),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ['services'] })
      qc.invalidateQueries({ queryKey: ['service', String(vars.serviceId)] })
    },
  })
}

// ---------- 开机启动 (autostart) ----------

/** 开机会为某服务加载的一个模型。vram_gb/gpu 后端按 registry spec 填,查不到就没有。 */
export interface PreloadModel {
  name: string
  vram_gb?: number | null
  gpu?: number | number[] | null
}

export interface AutostartPreview {
  service_id: string
  name: string
  autostart: boolean
  preload_models: PreloadModel[]
}

/** 「设为开机启动」确认框的数据源 —— 先问后端开机会加载什么,再让用户确认。
 *  故意做成裸函数而不是 useQuery:它只在点菜单那一下用一次,不需要缓存/订阅。 */
export function fetchAutostartPreview(serviceId: string | number) {
  return apiFetch<AutostartPreview>(`/api/v1/services/${serviceId}/autostart-preview`)
}

/** 人话版「35.0 GB · GPU 1」,两项都没有就返回空串。 */
export function preloadModelDetail(m: PreloadModel): string {
  const bits: string[] = []
  if (typeof m.vram_gb === 'number') bits.push(`${m.vram_gb} GB`)
  if (typeof m.gpu === 'number') bits.push(`GPU ${m.gpu}`)
  else if (Array.isArray(m.gpu) && m.gpu.length) bits.push(`GPU ${m.gpu.join(',')}`)
  return bits.join(' · ')
}

export interface SetAutostartVars {
  serviceId: string | number
  enabled: boolean
}

export function useSetServiceAutostart() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ serviceId, enabled }: SetAutostartVars) =>
      apiFetch<ServiceRow & { preload_models: PreloadModel[] }>(
        `/api/v1/services/${serviceId}/autostart`,
        { method: 'POST', body: JSON.stringify({ enabled }) },
      ),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ['services'] })
      qc.invalidateQueries({ queryKey: ['service', String(vars.serviceId)] })
    },
  })
}

export function useDeleteService() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (serviceId: string | number) =>
      apiFetch(`/api/v1/services/${serviceId}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['services'] })
    },
  })
}

// ---------- helpers ----------

export function paramKey(p: ExposedParam): string | undefined {
  return p.key ?? p.api_name
}

export function paramSlot(p: ExposedParam): string | undefined {
  return p.input_name ?? p.param_key
}

export function endpointFor(svc: Pick<ServiceRow, 'name' | 'category'>): string {
  // model-backed 服务按 category 给对应 OpenAI 兼容端点(2026-06-21:此前非 llm 全误落
  // /v1/apps/.../run —— embedding/tts/image/asr 服务卡显示的端点都是错的)。与 keys.ts
  // 的 endpointsFor 口径一致。workflow/app 类无 category 走 /v1/apps/.../run。
  const m = `model=${svc.name}`
  switch (svc.category) {
    case 'llm': return `POST /v1/chat/completions · ${m}`
    case 'embedding': return `POST /v1/embeddings · ${m}`
    case 'tts': return `POST /v1/audio/speech · ${m}`
    case 'asr': return `POST /v1/audio/transcriptions · ${m}`
    case 'image': return `POST /v1/images/generations · ${m}`
    default: return `POST /v1/apps/${svc.name}/run`
  }
}

export const NAME_RE = /^[a-z][a-z0-9-]{1,62}$/
