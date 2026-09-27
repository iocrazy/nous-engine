import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { apiFetch } from './client'
import { useToastStore } from '../stores/toast'
import { useLiveChannel } from './useLiveChannel'

// round3 #5:useEngines 在 Dashboard / Models / CreateServiceDialog 等多处同时挂载,
// 每个 consumer 的 onMessage 都会跑 → 同一条 model_status 事件弹多条相同 toast。
// 用模块级签名去重:相同 (model:status:detail) 在 1.5s 内只弹一次(invalidate 仍每个跑,
// 那是幂等的)。
let _lastEngineToast = { sig: '', at: 0 }
function _engineToastOnce(sig: string, msg: string, kind: 'success' | 'error') {
  const now = Date.now()
  if (sig === _lastEngineToast.sig && now - _lastEngineToast.at < 1500) return
  _lastEngineToast = { sig, at: now }
  useToastStore.getState().add(msg, kind)
}

export interface EngineInfo {
  name: string
  display_name: string
  type: string
  status: 'loaded' | 'unloaded' | 'loading' | 'failed'
  /** 主卡（GPU 组的第一张）。**永远是 int** —— 判"是不是组"只看 `gpus`。 */
  gpu: number
  /** GPU 组（张量并行）：[0, 2] = 这俩卡当一个单元用。null = 单卡。 */
  gpus?: number[] | null
  /** 该引擎的适配器接不接受 GPU 组（只有 vLLM / SGLang 这类子进程型 LLM 为 true）。
   *  false 时不显示「组合」子菜单项 —— 后端也会 400 拒绝。 */
  supports_gpu_group?: boolean
  vram_gb: number
  resident: boolean
  /** 统一引擎库:目录条目种类。model=整模型/引擎(可独立加载) upscale=SeedVR2 等 by-key
   *  超分(可独立加载,load 接入在 PR-3) component=单文件组件(随 pipeline 加载,不独立可加载)
   *  lora=LoRA(随模型加载)。缺省 model(向后兼容)。 */
  kind?: 'model' | 'upscale' | 'component' | 'lora'
  /** diffusion_models 单文件组件推断架构(z-image/flux2/anima)—— 预热时传给后端避免默认 flux2 错配。
   *  统一模型管理收尾 PR-2。非组件 / 无法推断 → null/undefined。 */
  arch?: string | null
  /** 已加载单文件组件的 L1 身份串(file|device|dtype|loras,含真实 device)。常驻 toggle 按它
   *  精确匹配,避 device='auto' 错配。未加载 / 非组件 → null。组件 L1 PR-3a。 */
  state_key?: string | null
  local_path: string | null
  local_exists: boolean
  // Remote metadata
  organization: string | null
  model_size: string | null
  frameworks: string[] | null
  libraries: string[] | null
  license: string | null
  languages: string[] | null
  tags: string[] | null
  tensor_types: string[] | null
  description: string | null
  has_metadata: boolean
  auto_detected: boolean
  /**
   * False = the model was discovered on disk but no adapter is wired up
   * (image / video diffusers right now). UI must disable the load
   * button — the backend will 422 with a config hint anyway, but it's
   * cleaner to gate the button than to let users click a doomed action.
   */
  has_adapter: boolean
  loaded_gpu: number | null
  loaded_gpus: number[] | null
  status_detail: string | null
}

/**
 * Subscribe to /ws/models and invalidate the ['engines'] query family.
 * Pure sync — no toasts. Use from canvas dropdowns that render inside
 * pages where useEngines() isn't mounted but still need live updates.
 *
 * The shared channel is URL-deduped so calling this from many sites
 * doesn't multiply socket connections.
 */
export function useEnginesLiveSync(): void {
  const qc = useQueryClient()
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = `${proto}//${window.location.host}/ws/models`
  useLiveChannel(url, {
    onMessage: (data) => {
      if (data.event === 'model_status') {
        qc.invalidateQueries({ queryKey: ['engines'] })
      }
    },
    onReconnect: () => qc.invalidateQueries({ queryKey: ['engines'] }),
  })
}

export function useEngines() {
  const qc = useQueryClient()
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = `${proto}//${window.location.host}/ws/models`

  useLiveChannel(url, {
    onMessage: (data) => {
      if (data.event !== 'model_status') return
      qc.invalidateQueries({ queryKey: ['engines'] })
      const sig = `${data.model}:${data.status}:${data.detail ?? ''}`
      if (data.status === 'loaded') {
        _engineToastOnce(sig, `${data.model} ${data.detail || '加载完成'}`, 'success')
      } else if (data.status === 'failed') {
        _engineToastOnce(sig, `${data.model} 加载失败: ${data.detail}`, 'error')
      } else if (data.status === 'installed') {
        _engineToastOnce(sig, `${data.model} 依赖安装完成`, 'success')
      } else if (data.status === 'install_failed') {
        _engineToastOnce(sig, `${data.model} 依赖安装失败: ${data.detail}`, 'error')
      }
    },
    onReconnect: () => qc.invalidateQueries({ queryKey: ['engines'] }),
  })

  return useQuery({
    queryKey: ['engines'],
    queryFn: () => apiFetch<EngineInfo[]>('/api/v1/engines'),
    // Every status transition arrives via /ws/models; the periodic
    // refetch is a safety net (60s) for the rare case the socket is
    // wedged in some half-open state the browser hides from us.
    refetchInterval: (query) => query.state.status === 'error' ? 10_000 : 60_000,
    refetchOnWindowFocus: false,
    retry: false,
    // Show last-known engines instantly when navigating back to the page;
    // background refetch keeps them fresh. Backend serves cached body in <50ms
    // when warm, so the visible flicker window collapses.
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

export function useLoadEngine() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch(`/api/v1/engines/${name}/load`, { method: 'POST' }),
    onSuccess: (_, name) => {
      // Immediately invalidate to show "loading" status; terminal toast comes from WebSocket
      qc.invalidateQueries({ queryKey: ['engines'] })
      useToastStore.getState().add(`${name} 开始加载...`, 'info')
    },
    onError: (error: Error) => {
      useToastStore.getState().add(`加载失败: ${error.message}`, 'error')
    },
  })
}

export function useUnloadEngine() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch(`/api/v1/engines/${name}/unload?force=true`, { method: 'POST' }),
    onSuccess: (_, name) => {
      qc.invalidateQueries({ queryKey: ['engines'] })
      useToastStore.getState().add(`${name} 已卸载`, 'success')
    },
    onError: (error: Error) => {
      useToastStore.getState().add(`卸载失败: ${error.message}`, 'error')
    },
  })
}

export function useSyncMetadata() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () =>
      apiFetch('/api/v1/engines/sync-metadata', { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engines'] }),
  })
}

export function useSetResident() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, resident }: { name: string; resident: boolean }) =>
      apiFetch(`/api/v1/engines/${name}/resident?resident=${resident}`, { method: 'PATCH' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engines'] }),
    onError: (error: Error) => {
      useToastStore.getState().add(`设置失败: ${error.message}`, 'error')
    },
  })
}

// 每模型显存预算(spec 2026-06-13 PR-2)。overlay {mode,value} → vLLM gpu_memory_utilization。
export type VramBudgetMode = 'auto' | 'percent' | 'absolute'
export interface VramBudget {
  mode: VramBudgetMode
  value?: number
}
export interface VramBudgetInfo {
  name: string
  applicable: boolean
  current: VramBudget
  recommended_gb: number
  recommended_percent: number | null
  card_total_gb: number
  yaml_gpu_memory_utilization: number | null
}

/** 拉某引擎当前显存预算设置 + 推荐值 + 卡总显存。仅在弹窗打开(name 非空)时请求。 */
export function useVramBudget(name: string | null) {
  return useQuery({
    queryKey: ['vram-budget', name],
    queryFn: () => apiFetch<VramBudgetInfo>(`/api/v1/engines/${name}/vram-budget`),
    enabled: !!name,
    staleTime: 0,
  })
}

/** 写显存预算 overlay。需 unload + load 重载模型生效(响应带 hint)。 */
export function useSetVramBudget() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, mode, value }: { name: string; mode: VramBudgetMode; value?: number }) =>
      apiFetch<{ name: string; vram_budget: VramBudget; hint: string }>(
        `/api/v1/engines/${name}/vram-budget`,
        { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode, value }) },
      ),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['vram-budget', data.name] })
      qc.invalidateQueries({ queryKey: ['engines'] })
      useToastStore.getState().add(`显存预算已保存 · ${data.hint}`, 'success')
    },
    onError: (error: Error) => {
      useToastStore.getState().add(`保存失败: ${error.message}`, 'error')
    },
  })
}

export function useScanModels() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () =>
      apiFetch<{
        count: number
        local_available?: number
        not_local?: number
        models: string[]
      }>('/api/v1/engines/scan', { method: 'POST' }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['engines'] })
      // PR-11:旧文案「扫描完成,共 N 个模型」实际是 yaml 配置识别数,会比
      // 引擎库可见(`/api/v1/engines` 过滤 local_path 后)多出未下载的份额 —
      // 用户曾报告「扫到 25 实际只能用 16」之误导。后端 PR-11 同时返回
      // local_available/not_local,有差异时显式拆分提示。
      // 兼容旧后端 payload(无 local_available 字段时降级为原文案)。
      const total = data.count
      const local = data.local_available
      const missing = data.not_local
      const msg =
        local != null && missing != null && missing > 0
          ? `识别 ${total} 个模型 · 本地可用 ${local} · ${missing} 个未下载`
          : `扫描完成,共 ${total} 个模型`
      useToastStore.getState().add(msg, 'success')
    },
    onError: (error: Error) => {
      useToastStore.getState().add(`扫描失败: ${error.message}`, 'error')
    },
  })
}

export function useRefreshMetadata() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch(`/api/v1/engines/${name}/refresh-metadata`, { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engines'] }),
  })
}

/** Bug 3 PR-2c:runner 子进程里加载的 combo adapter 实体(image/tts)。它们是工作流
 * 动态组装的单文件 combo,不对应注册卡片,所以独立于 EngineInfo,在引擎库「已加载」
 * tab 单独渲染。数据来自 /api/v1/engines/loaded-adapters(聚合各 runner 的 Pong 快照)。 */
export interface LoadedAdapter {
  model_id: string
  model_type: string
  group_id: string
  gpu_index: number | null
  vram_mb: number | null
  pipeline_class: string | null
  source_files: string[]
  display_name: string
  last_used_ago_sec: number | null
}

export function useLoadedAdapters() {
  return useQuery({
    queryKey: ['loaded-adapters'],
    queryFn: () =>
      apiFetch<{ count: number; entries: LoadedAdapter[] }>(
        '/api/v1/engines/loaded-adapters',
      ),
    // 后端快照由 runner 节点完成时即时 reconcile(PR-2b);这里 8s 兜底轮询。
    refetchInterval: 8000,
    refetchOnWindowFocus: false,
    retry: false,
    staleTime: 4000,
  })
}

/** 卸载已加载的 combo adapter(引擎库「已加载」卡卸载按钮,统一模型管理收尾 PR-2)。 */
export function useUnloadAdapter() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ model_id }: { model_id: string }) =>
      apiFetch('/api/v1/engines/loaded-adapter/unload', {
        method: 'POST',
        body: JSON.stringify({ model_id }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['loaded-adapters'] })
      qc.invalidateQueries({ queryKey: ['engines'] })
      useToastStore.getState().add('已加载模型开始卸载...（几秒后刷新）', 'info')
    },
    onError: (error: Error) => {
      useToastStore.getState().add(`卸载失败: ${error.message}`, 'error')
    },
  })
}

export interface GpuDevice {
  index: number
  name: string
  vram_gb: number
}

/**
 * 可做张量并行的 GPU 组候选。**来源是后端 configs/hardware.yaml 里声明过的多卡 group**
 * （不是前后端枚举卡的组合）—— 那份拓扑记着"哪张卡在驱动显示器"这类运维约束。
 * yaml 没声明多卡组时返回空数组 + hint，菜单里就不出现「组合」项。
 */
export interface GpuGroup {
  id?: string
  gpus: number[]
  name: string
  nvlink: boolean
  total_gb: number
  /** 组里正在驱动显示服务的卡 —— 有值就在菜单里标记出来（重负载会挤崩桌面）。 */
  display_gpus?: number[]
}

export function useGpus() {
  return useQuery({
    queryKey: ['gpus'],
    queryFn: () =>
      apiFetch<{ count: number; devices: GpuDevice[] }>('/api/v1/engines/gpus'),
  })
}

/** GPU 组候选。异构组合永远不会出现在这里 —— 后端按型号分组后才枚举。 */
export function useGpuGroups() {
  return useQuery({
    queryKey: ['gpu-groups'],
    queryFn: () => apiFetch<{ groups: GpuGroup[]; hint?: string }>('/api/v1/gpu/groups'),
    staleTime: 5 * 60_000,  // 机器上的卡不会中途变
  })
}

/**
 * 分配 GPU：单卡 `{gpu}` 或 GPU 组 `{gpus}`（张量并行）。
 * 后端两条路都走 PATCH /api/v1/engines/{name}/gpu —— 单卡用查询参数（老路径不变），
 * 组走请求体。设单卡会显式清掉已有的组。
 */
export function useSetGpu() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, gpu, gpus }: { name: string; gpu?: number; gpus?: number[] }) =>
      gpus && gpus.length > 1
        ? apiFetch(`/api/v1/engines/${name}/gpu`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ gpus }),
          })
        : apiFetch(`/api/v1/engines/${name}/gpu?gpu=${gpu ?? 0}`, { method: 'PATCH' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engines'] }),
    onError: (error: Error) => {
      useToastStore.getState().add(`GPU 分配失败: ${error.message}`, 'error')
    },
  })
}

// ── 物理删除(spec 2026-07-28-model-physical-delete)────────────────────────
// 两步式:先 preflight 拿「将删什么、将释放多少、谁在挡、源码还有哪些残留引用」,
// 前端据此渲染确认框;再 delete 真删。后端会自己重跑一遍预检,前端这份只用于展示。

export interface CodeRef {
  file: string
  line: number
  text: string
}

export interface DeletePreflight {
  name: string
  kind: string
  target_path: string
  is_dir: boolean
  size_bytes: number
  blockers: {
    /** 非 null = 硬阻断(还在显存里),force 也不放行 */
    loaded: { status: string; gpu: number | null } | null
    /** 引用该模型的服务实例 —— 软阻断,勾确认后带 force 放行 */
    services: { id: string; name: string }[]
  }
  registry_cleanup: {
    models_d_yaml: string | null
    model_metadata: boolean
    runtime_overrides: number
  }
  code_refs: CodeRef[]
  code_refs_truncated: boolean
  code_refs_error: string | null
}

export interface DeleteResult {
  deleted: boolean
  name: string
  target_path: string
  freed_bytes: number
  /** 非空 = 磁盘只删掉一部分(权限/占用),如实展示,不静默 */
  disk_errors: string[]
  registry_cleaned: {
    models_d_yaml: boolean
    model_metadata: boolean
    runtime_overrides: number
  }
  code_refs: CodeRef[]
  code_refs_truncated: boolean
  code_refs_error: string | null
}

export function useDeletePreflight(name: string | null) {
  return useQuery({
    queryKey: ['engine-delete-preflight', name],
    enabled: !!name,
    // 预检要 du 整个模型目录 + git grep 仓库,别缓存太久也别自动重跑。
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    queryFn: () =>
      apiFetch<DeletePreflight>('/api/v1/engines/delete/preflight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      }),
  })
}

export function useDeleteEngine() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, force }: { name: string; force?: boolean }) =>
      apiFetch<DeleteResult>('/api/v1/engines/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, force: !!force }),
      }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['engines'] })
      // 删模型不动服务实体,但服务卡上的「模型状态」徽标会变 —— 一并刷新。
      qc.invalidateQueries({ queryKey: ['services'] })
      const gb = data.freed_bytes / 1024 ** 3
      const freed = gb >= 0.1 ? `${gb.toFixed(1)}GB` : `${Math.round(data.freed_bytes / 1024 ** 2)}MB`
      if (data.disk_errors.length > 0) {
        useToastStore.getState().add(
          `${data.name} 部分删除:${data.disk_errors.length} 项删不掉,已释放 ${freed}`, 'error')
      } else {
        useToastStore.getState().add(`已删除 ${data.name}，释放 ${freed}`, 'success')
      }
    },
    onError: (error: Error) => {
      useToastStore.getState().add(`删除失败: ${error.message}`, 'error')
    },
  })
}
