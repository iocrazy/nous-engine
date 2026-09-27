import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './client'

// ---------- shared types ----------

export interface ComfyExposedParam {
  key: string
  label?: string
  type?: string
  default?: unknown
  min?: number
  max?: number
  step?: number
  options?: unknown[]
  required?: boolean
  random?: boolean
  multiple?: boolean
  /** 选项依赖:本字段的选项清单随**另一个 exposed_param 的当前值**变(写它的 key)。 */
  options_depends_on?: string | null
  /** 去哪儿取那份清单。目前只有 `comfy_styles`(ComfyUI-Easy-Use 风格清单)。 */
  options_source?: 'comfy_styles' | null
  /** 文件类可选参数:调用方没传时,桥把它的 LoadImage 连同只为它服务的下游支路从图里剪掉
   *  (而不是把模板里的占位图喂进模型)。只对文件类、非必填字段有效,后端校验。 */
  omit_when_empty?: boolean
  comfy_node_id: string
  comfy_input: string
}

export interface ComfyTemplateRow {
  id: string
  name: string
  service_name: string | null
  node_count: number
  exposed_count: number
}

export interface ComfyTemplateDetail {
  id: string
  name: string
  service_name: string
  workflow_json: Record<string, unknown>
  exposed_params: ComfyExposedParam[]
}

export interface CreateComfyTemplateResult {
  id: string
  name: string
  service_name: string
  node_count: number
}

export interface ComfyDevice {
  name: string
  type: string
  index: number
  vram_total: number
  vram_free: number
  /** **整卡**已用(vram_total - vram_free)——含同卡其它进程,不是 ComfyUI 占的。 */
  vram_used: number
  /** **ComfyUI 自占**(torch reserved)——「释放显存」能动的只有这部分。 */
  comfy_used: number
  torch_vram_total: number
}

export interface ComfyHealth {
  online: boolean
  queue_depth: number
  version: string
  base_url: string
  timeout_s: number
  /** 后端恒返回此键(sidecar 离线时为 [])——不设 optional,否则漏字段的 mock/调用方
   *  不会被类型检查逮到(vram-panel 交付时因文件被占用临时放宽,现收紧)。 */
  devices: ComfyDevice[]
}

export interface FreeComfyVramResult {
  ok: boolean
  /** 显存是否已真降下来。ComfyUI 的 /free 是异步的(只设 flag,worker 唤醒后才卸载),
   *  后端轮询至多 6s;超时仍未降则 false —— 此时 UI 该说"已触发,可能仍在进行"。 */
  settled: boolean
  /** 本次实际归还的字节数(未落定时为 0)。 */
  freed_bytes: number
  devices: ComfyDevice[]
}

// ---------- calls ----------

export function createComfyTemplate(
  name: string,
  workflow: Record<string, unknown>,
): Promise<CreateComfyTemplateResult> {
  return apiFetch<CreateComfyTemplateResult>('/api/v1/comfy-templates', {
    method: 'POST',
    body: JSON.stringify({ name, workflow }),
  })
}

export function putMapping(
  id: string | number,
  exposedParams: ComfyExposedParam[],
): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/api/v1/comfy-templates/${id}/mapping`, {
    method: 'PUT',
    body: JSON.stringify({ exposed_params: exposedParams }),
  })
}

export function getComfyTemplate(id: string | number): Promise<ComfyTemplateDetail> {
  return apiFetch<ComfyTemplateDetail>(`/api/v1/comfy-templates/${id}`)
}

export function listComfyTemplates(): Promise<ComfyTemplateRow[]> {
  return apiFetch<ComfyTemplateRow[]>('/api/v1/comfy-templates')
}

export function reuploadComfyTemplate(
  id: string | number,
  workflow: Record<string, unknown>,
): Promise<{ stale_keys: string[] }> {
  return apiFetch<{ stale_keys: string[] }>(`/api/v1/comfy-templates/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ workflow }),
  })
}

export function getComfyHealth(): Promise<ComfyHealth> {
  return apiFetch<ComfyHealth>('/api/v1/comfy/health')
}

export function getObjectInfo(): Promise<Record<string, unknown>> {
  return apiFetch<Record<string, unknown>>('/api/v1/comfy/object-info')
}

export function freeComfyVram(): Promise<FreeComfyVramResult> {
  return apiFetch<FreeComfyVramResult>('/api/v1/comfy/free', { method: 'POST' })
}


// ---------- 动态选项(选项依赖)----------

/** `GET /api/v1/comfy/styles` 归一后的选项项 —— 与 exposed_params.options 同形。 */
export interface ComfyStyleOption {
  value: string
  label?: string
  image?: string
}

export function getComfyStyles(pack: string): Promise<{ pack: string; options: ComfyStyleOption[] }> {
  return apiFetch(`/api/v1/comfy/styles?pack=${encodeURIComponent(pack)}`)
}

/** 某个风格包的风格清单。`pack` 为空(还没选包)时不发请求。
 *
 *  queryKey 带上 pack —— 切包就是换一个缓存条目,来回切不会重复打 sidecar。
 *  `retry: false`:sidecar 离线时立刻让 UI 退回静态清单,而不是转三次圈才认输。 */
export function useComfyStyles(pack: string | undefined) {
  return useQuery({
    queryKey: ['comfy-styles', pack],
    queryFn: () => getComfyStyles(pack as string),
    enabled: Boolean(pack),
    staleTime: 5 * 60_000,
    retry: false,
  })
}
