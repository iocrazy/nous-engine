import { useState, useCallback, useEffect } from 'react'
import { Copy, Check, X, Search, Pin, PinOff } from 'lucide-react'
import {
  useEngines, useLoadEngine, useUnloadEngine, useSyncMetadata,
  useScanModels, useSetResident, useRefreshMetadata, useGpus, useGpuGroups, useSetGpu,
  useLoadedAdapters, usePreloadSeedvr2, useUnloadSeedvr2, useUnloadAdapter,
  useSetSeedvr2Resident, usePreloadComponent, useSetComponentResident, useUnloadComponent,
  useVramBudget, useSetVramBudget,
  type EngineInfo, type LoadedAdapter, type VramBudgetMode, type VramBudgetInfo,
} from '../../api/engines'
import { apiFetch } from '../../api/client'
import { useToastStore } from '../../stores/toast'
import ContextMenu, { type MenuItem } from '../ui/ContextMenu'
import DeleteModelDialog from '../models/DeleteModelDialog'
import { buildGpuAssignSubmenu } from '../models/gpuAssignMenu'
import { LaunchParamsEditor } from '../engines/LaunchParamsEditor'
import { useLaunchParams } from '../../api/vllm'

const TYPE_LABELS: Record<string, string> = {
  llm: '语言模型 LLM',
  embedding: '向量嵌入 Embedding',
  tts: '语音合成 TTS',
  asr: '语音识别 ASR',
  image: '图像生成 Image',
  video: '视频生成 Video',
  understand: '多模态理解 VL',
}

const TYPE_ORDER = ['llm', 'embedding', 'tts', 'asr', 'image', 'video', 'understand']

// 走 vLLM 适配器的类目 —— 只有它们有 gpu_memory_utilization 旋钮可配显存预算(spec 2026-06-13)。
// asr(Qwen3-ASR)也走 vLLM,同样可配显存预算。
const VLLM_TYPES = new Set(['llm', 'embedding', 'understand', 'vl', 'asr'])

// m11-style tag colors per model type — keeps semantic differentiation
// without the eye-watering rainbow of the legacy "everything is a chip" UI.
const TYPE_TAG_STYLE: Record<string, { bg: string; color: string }> = {
  llm:        { bg: 'var(--accent-2-subtle)', color: 'var(--accent-2)' },                           // green/teal
  tts:        { bg: 'rgba(168,85,247,0.15)',  color: 'rgb(196, 154, 247)' },                        // purple
  image:      { bg: 'rgba(20,184,166,0.18)',  color: 'var(--accent-2)' },                           // teal (slightly stronger)
  video:      { bg: 'rgba(244,114,182,0.15)', color: 'rgb(244,114,182)' },                          // pink
  understand: { bg: 'rgba(59,130,246,0.15)',  color: 'var(--info, #3b82f6)' },                      // blue
  embedding:  { bg: 'rgba(234,179,8,0.15)',   color: 'rgb(234,179,8)' },                            // yellow
  asr:        { bg: 'rgba(14,165,233,0.15)',  color: 'rgb(14,165,233)' },                           // sky
}

// Short labels for tabs (vs full names in section headers)
const TAB_LABELS: Record<string, string> = {
  llm: '语言模型',
  embedding: '向量',
  tts: '语音合成',
  asr: '语音识别',
  image: '图像',
  video: '视频',
  understand: '视觉',
}

type TabId = 'all' | typeof TYPE_ORDER[number]

interface ContextMenuState {
  visible: boolean
  position: { x: number; y: number }
  model: EngineInfo | null
}

export default function ModelsOverlay() {
  const { data: engines, isLoading, isError } = useEngines()
  const { data: loadedAdaptersData } = useLoadedAdapters()
  const loadEngine = useLoadEngine()
  const unloadEngine = useUnloadEngine()
  const preloadSeedvr2 = usePreloadSeedvr2()
  const unloadSeedvr2 = useUnloadSeedvr2()
  const setSeedvr2Resident = useSetSeedvr2Resident()
  const preloadComponent = usePreloadComponent()
  const unloadComponent = useUnloadComponent()
  const setComponentResident = useSetComponentResident()
  const syncMeta = useSyncMetadata()
  const scanModels = useScanModels()
  const setResident = useSetResident()
  const refreshMeta = useRefreshMetadata()
  const { data: gpuData } = useGpus()
  const setGpu = useSetGpu()
  const { data: gpuGroupData } = useGpuGroups()

  const [ctxMenu, setCtxMenu] = useState<ContextMenuState>({
    visible: false,
    position: { x: 0, y: 0 },
    model: null,
  })
  const [activeTab, setActiveTab] = useState<TabId>('all')
  // 显存预算弹窗目标(vLLM 类引擎),null = 关闭。
  const [budgetTarget, setBudgetTarget] = useState<EngineInfo | null>(null)
  // 启动参数弹窗目标(vLLM 类引擎),null = 关闭。与显存预算分开两栏:
  // 前者写 params(上下文/并发),后者写绝对 GiB —— 两条端点、两套语义。
  const [paramsTarget, setParamsTarget] = useState<EngineInfo | null>(null)
  // 物理删除确认框目标,null = 关闭。
  const [deleteTarget, setDeleteTarget] = useState<EngineInfo | null>(null)
  // 图像 tab 下的二级子 tab —— 按**文件夹/角色**分:整模型 / 超分 / diffusion_models / clip / vae / loras。
  const [imageBucket, setImageBucket] = useState<string>('all')
  // 跨 tab/桶的名称搜索 —— 在当前可见列表里再按 display_name/name/路径 子串过滤。统一模型管理收尾 PR-3。
  const [search, setSearch] = useState('')

  const closeMenu = useCallback(() => {
    setCtxMenu((prev) => ({ ...prev, visible: false }))
  }, [])

  const handleContextMenu = useCallback((e: React.MouseEvent, model: EngineInfo) => {
    e.preventDefault()
    setCtxMenu({ visible: true, position: { x: e.clientX, y: e.clientY }, model })
  }, [])

  const handleToggle = useCallback(
    (engine: EngineInfo) => {
      if (engine.status === 'loading') return // ignore while loading
      // 统一引擎库 PR-3:超分(SeedVR2)从引擎库直接预热/卸载(by-key,经 image runner)。
      if (engine.kind === 'upscale') {
        if (engine.status === 'loaded') unloadSeedvr2.mutate(engine.name)
        else preloadSeedvr2.mutate(engine.name)
        return
      }
      // 组件(diffusion_models/clip/vae)可从引擎库预加载进显存 / 卸载(组件 L1 PR-2a + 统一模型管理
      // 收尾 PR-1)。已加载 → 出缓存释放显存(state_key 精确匹配;combo 在用则只清常驻待自然释放)。
      // LoRA 仍随 pipeline,不独立预加载。
      if (engine.kind === 'component') {
        if (engine.status === 'loaded') {
          if (engine.state_key) unloadComponent.mutate({ state_key: engine.state_key })
          else unloadComponent.mutate({ name: engine.name })
        } else {
          preloadComponent.mutate({ name: engine.name, arch: engine.arch })
        }
        return
      }
      if (engine.kind === 'lora') {
        useToastStore.getState().add(
          `${engine.display_name} 是 LoRA，随图像 pipeline 加载，不能独立预加载`, 'info')
        return
      }
      if (engine.status === 'loaded') {
        unloadEngine.mutate(engine.name)
        return
      }
      if (!engine.has_adapter) {
        // Auto-detected diffusers without an adapter — backend would 422
        // anyway. Surface the same hint without making the request.
        useToastStore.getState().add(
          `${engine.name} 未注册：图像/视频 adapter 未实现，需要先在 backend/configs/models.yaml 添加 adapter`,
          'error',
        )
        return
      }
      loadEngine.mutate(engine.name)
    },
    [loadEngine, unloadEngine, preloadSeedvr2, unloadSeedvr2, preloadComponent, unloadComponent],
  )

  // 常驻 toggle 按 kind 分派:组件走组件 L1 端点(用 state_key 精确匹配)、SeedVR2 走 by-key
  // 端点、其余(registry 整模型)走老 yaml /resident。组件 L1 PR-3b。
  const handleToggleResident = useCallback(
    (engine: EngineInfo) => {
      const next = !engine.resident
      if (engine.kind === 'component') {
        if (engine.state_key) setComponentResident.mutate({ state_key: engine.state_key, resident: next })
        else setComponentResident.mutate({ name: engine.name, resident: next })  // 未加载:按 name(auto)
        return
      }
      if (engine.kind === 'upscale') {
        setSeedvr2Resident.mutate({ name: engine.name, resident: next })
        return
      }
      setResident.mutate({ name: engine.name, resident: next })
    },
    [setComponentResident, setSeedvr2Resident, setResident],
  )

  const hasAnyMissing = (engines ?? []).some((e) => !e.has_metadata)

  // 统一引擎库:catalog 扩展条目(超分/组件/LoRA)—— 非 registry 模型,resident/GPU/API/元数据
  // 等操作不适用,菜单里禁用(载/卸经 handleToggle 给提示)。
  const isExtra = !!(ctxMenu.model?.kind && ctxMenu.model.kind !== 'model')
  const isComponent = ctxMenu.model?.kind === 'component'
  const isUpscale = ctxMenu.model?.kind === 'upscale'
  const cmLoaded = ctxMenu.model?.status === 'loaded'
  // 组件常驻只对「已加载」有意义(未加载组件 toggle 用 name+auto 匹配不上 L1);registry 整模型的
  // resident 是 yaml 自动加载,与是否加载无关;SeedVR2 by-key 需先加载才有 model_id 可 pin。
  const residentDisabled =
    ctxMenu.model?.kind === 'lora'
    || (isComponent && !cmLoaded)
    || (isUpscale && !cmLoaded)
  // Build context menu items for the active model
  const menuItems: MenuItem[] = ctxMenu.model
    ? [
        {
          label: isComponent
            ? (cmLoaded ? '卸载（出缓存释放显存）' : '预加载到显存（自动选卡）')
            : isUpscale ? (cmLoaded ? '卸载 SeedVR2' : '加载 SeedVR2')
            : ctxMenu.model.kind === 'lora' ? 'LoRA · 随模型加载'
            : ctxMenu.model.status === 'loaded' ? '卸载模型'
            : ctxMenu.model.status === 'loading' ? '加载中...'
            : !ctxMenu.model.has_adapter ? '未注册（无 adapter）'
            : '加载模型',
          onClick: () => handleToggle(ctxMenu.model!),
          disabled:
            ctxMenu.model.status === 'loading'
            || ctxMenu.model.kind === 'lora'
            || (!isExtra && ctxMenu.model.status !== 'loaded' && !ctxMenu.model.has_adapter),
        },
        // 组件预加载:可选落哪张卡(自动选卡之外,直接指定 GPU)。bfloat16 默认精度。
        ...(isComponent && !cmLoaded && (gpuData?.devices ?? []).length > 0
          ? [{
              label: '预加载到指定 GPU',
              submenu: (gpuData?.devices ?? []).map((g) => ({
                label: `GPU ${g.index}: ${g.name}`,
                onClick: () => preloadComponent.mutate({
                  name: ctxMenu.model!.name, device: `cuda:${g.index}`, arch: ctxMenu.model!.arch,
                }),
              })),
            } as MenuItem]
          : []),
        // 「预加载 + 常驻」一步到位(自动选卡)。**不给选精度** —— 组件预加载固定用标准 bf16 计算
        // 精度:文件存储格式名字里写死(bf16/fp8mixed…),而单组件 build_bridged 路径不做 fp8 torchao
        // 量化(那只在整 pipeline _ensure_pipe 做),选 fp8 只会静默落 bf16 误导用户。省显存的 fp8
        // 走「跑工作流时 loader 节点选 weight_dtype」,不在引擎库预加载这条路。
        ...(isComponent && !cmLoaded
          ? [{
              label: '预加载到显存 + 常驻（自动选卡）',
              onClick: () => preloadComponent.mutate({ name: ctxMenu.model!.name, resident: true, arch: ctxMenu.model!.arch }),
            } as MenuItem]
          : []),
        {
          label: ctxMenu.model.resident
            ? (isExtra ? '取消常驻' : '取消自动加载')
            : (isExtra ? '设为常驻' : '设为自动加载'),
          onClick: () => handleToggleResident(ctxMenu.model!),
          disabled: residentDisabled,
        },
        // GPU 分配 / 创建 API / 刷新元数据 只对**已注册整模型**适用(改 yaml / 起 instance / 拉元数据)——
        // 组件/LoRA/超分这些 catalog 条目用不上,以前显示但全灰会让人困惑(用户:为啥 GPU 分配点不了)。
        // 整段对 isExtra 隐藏;组件选卡走上面的「预加载到指定 GPU」。组件 L1 PR。
        ...(!isExtra
          ? [
        { label: '', divider: true },
        {
          label: 'GPU 分配',
          disabled: false,
          // 单卡 + 组合（张量并行）。组合项来自 /api/v1/gpu/groups（只列同型号组合）。
          submenu: buildGpuAssignSubmenu({
            devices: gpuData?.devices ?? [],
            groups: gpuGroupData?.groups ?? [],
            currentGpu: typeof ctxMenu.model!.gpu === 'number' ? ctxMenu.model!.gpu : undefined,
            currentGroup: ctxMenu.model!.gpus,
            supportsGroup: ctxMenu.model!.supports_gpu_group,
            onPickGpu: (gpu) => setGpu.mutate({ name: ctxMenu.model!.name, gpu }),
            onPickGroup: (gpus) => setGpu.mutate({ name: ctxMenu.model!.name, gpus }),
          }),
        },
        { label: '', divider: true },
        {
          label: '创建 API 接入点',
          onClick: async () => {
            const model = ctxMenu.model!
            // service name = 客户端请求里要传的 model 标识,必须匹配 ^[a-z][a-z0-9-]{1,62}$
            // (旧代码用「${display_name} API」带空格/大写 → 违反 ck_service_instances_name_fmt 直接 500)。
            // slug 化 + 短后缀保唯一(name 有 UNIQUE 约束,重复建会 409/500)。
            const slug = (model.name || model.display_name)
              .toLowerCase()
              .replace(/[^a-z0-9-]+/g, '-')
              .replace(/^[^a-z]+/, '')
              .replace(/-+$/g, '')
              .slice(0, 50) || 'model'
            const serviceName = `${slug}-${Date.now().toString(36).slice(-4)}`
            try {
              // 1) 把模型登记成服务(v3 /services/register-model,双轨收敛 #3;
              //    M:N 解析按 name 匹配 request.model)
              const instance = await apiFetch<{ id: string }>('/api/v1/services/register-model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  source_name: model.name,
                  name: serviceName,
                  type: model.type,
                }),
              })
              // 2) M:N 建 key + 一键授权 grant(取代 legacy 1:1 /instances/{id}/keys)
              const keyResult = await apiFetch<{ secret: string; id: string }>('/api/v1/keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ label: `${model.display_name} default`, service_ids: [instance.id] }),
              })
              // M:N key 需在请求里带 model=<服务名>,所以连服务名一起给出
              window.prompt(
                `API 接入点已创建(Ctrl+C 复制 key)。请求时 model 传 "${serviceName}",Authorization: Bearer <key>:`,
                keyResult.secret,
              )
              useToastStore.getState().add(`接入点已创建: ${model.display_name}`, 'success')
            } catch (e: any) {
              useToastStore.getState().add(`创建失败: ${e.message}`, 'error')
            }
          },
          disabled: isExtra || ctxMenu.model.status !== 'loaded',
        },
        { label: '', divider: true },
        {
          label: '刷新元数据',
          onClick: () => refreshMeta.mutate(ctxMenu.model!.name),
          disabled: false,
        },
        // 显存预算:只对 vLLM 类(llm/embedding/vl)开放 —— image/tts 走别的机制。
        ...(VLLM_TYPES.has(ctxMenu.model.type)
          ? [
              { label: '', divider: true } as MenuItem,
              {
                label: '显存预算…',
                onClick: () => setBudgetTarget(ctxMenu.model!),
                disabled: false,
              } as MenuItem,
              {
                label: '启动参数…',
                onClick: () => setParamsTarget(ctxMenu.model!),
                disabled: false,
              } as MenuItem,
            ]
          : []),
          ] as MenuItem[]
          : []),
        { label: '', divider: true },
        {
          // 物理删除(spec 2026-07-28):rm -rf 磁盘 + 清注册表。5 类条目都可删,
          // 阻断判定(已加载 / 被服务引用)在确认框里由后端预检给出,不在菜单层猜。
          label: '删除（物理删除文件）',
          danger: true,
          onClick: () => setDeleteTarget(ctxMenu.model!),
        },
      ]
    : []

  // Compute tab counts (m11 style — single flat grid filtered by tab,
  // not the per-type sectioned rendering of the previous IA).
  const allEngines = engines ?? []
  // Bug 3 PR-2c:runner 里加载的 combo adapter 实体(image/tts),不在 engines 注册卡里。
  const loadedAdapters = loadedAdaptersData?.entries ?? []
  const typeCounts: Record<string, number> = {}
  for (const e of allEngines) typeCounts[e.type] = (typeCounts[e.type] ?? 0) + 1

  // 图像条目归到哪个「桶」(文件夹/角色):整模型/超分用 kind;组件/LoRA 用文件夹(从 name
  // "component:<role>:<path>" 取 role:diffusion_models/clip/vae/loras)。
  const imageBucketOf = (e: EngineInfo): string => {
    if (e.kind === 'model' || e.kind === 'upscale') return e.kind
    if (e.name.startsWith('component:')) return e.name.split(':')[1] || 'component'
    return e.kind ?? 'component'
  }
  const imageEngines = allEngines.filter((e) => e.type === 'image')
  const bucketCounts: Record<string, number> = { all: imageEngines.length }
  for (const e of imageEngines) {
    const b = imageBucketOf(e)
    bucketCounts[b] = (bucketCounts[b] ?? 0) + 1
  }
  // 「已加载」快速筛(用户 2026-06-11):紧跟「全部」,在**当前 tab 内**按 status 过滤,
  // 不用切去顶层「已加载」tab(那个跨全类型)。所有类型 tab 通用;图像 tab 额外有桶。
  bucketCounts.loaded = imageEngines.filter((e) => e.status === 'loaded').length
  // 子 tab 顺序:整模型 → 超分 → 各文件夹。label 友好化。
  // clip 角色对齐 ComfyUI「Load CLIP」节点,但文件实际在 media/text_encoders/ —— 标签用「文本编码器」
  // 对齐文件夹,免「为啥叫 CLIP 不是 text_encoders」的困惑(底层角色 key 仍是 clip,扫描/端点不变)。
  const BUCKET_LABEL: Record<string, string> = {
    model: '整模型', upscale: '超分', diffusion_models: 'diffusion_models',
    clip: '文本编码器', vae: 'VAE', loras: 'LoRA',
  }
  const BUCKET_ORDER = ['model', 'upscale', 'diffusion_models', 'clip', 'vae', 'loras']
  const imageSubTabs = [
    { id: 'all', label: '全部' },
    { id: 'loaded', label: '已加载' },
    ...BUCKET_ORDER.filter((b) => (bucketCounts[b] ?? 0) > 0).map((b) => ({ id: b, label: BUCKET_LABEL[b] ?? b })),
  ]
  // 非图像 tab(全部/语言模型/语音合成/视觉…)的通用子筛:全部 / 已加载。计数按当前 tab 范围;
  // 「全部」tab 的已加载额外计入 runner combo adapter 实体(与原顶层「已加载」tab 口径一致)。
  const typeTabEngines = activeTab === 'all'
    ? allEngines
    : allEngines.filter((e) => e.type === activeTab)
  const genericSubTabs = [
    { id: 'all', label: '全部', count: typeTabEngines.length },
    {
      id: 'loaded', label: '已加载',
      count: typeTabEngines.filter((e) => e.status === 'loaded').length
        + (activeTab === 'all' ? loadedAdapters.length : 0),
    },
  ]
  // 「已加载」子筛下额外列 runner combo adapter 实体(原顶层 tab 行为):全部/图像 tab 适用
  // (combo 是 image/tts 实体,全部 tab 必含;图像 tab 也展示)。
  const showAdapters = imageBucket === 'loaded' && (activeTab === 'all' || activeTab === 'image')

  const visibleEngines = (() => {
    let list: EngineInfo[]
    if (activeTab === 'all') {
      list = imageBucket === 'loaded' ? allEngines.filter((e) => e.status === 'loaded') : allEngines
    } else {
      list = allEngines.filter((e) => e.type === activeTab)
      if (imageBucket !== 'all') {
        // 「已加载」对所有类型 tab 通用;桶(整模型/VAE/LoRA…)仅图像 tab 有意义。
        if (imageBucket === 'loaded') list = list.filter((e) => e.status === 'loaded')
        else if (activeTab === 'image') list = list.filter((e) => imageBucketOf(e) === imageBucket)
      }
    }
    const q = search.trim().toLowerCase()
    if (q) {
      list = list.filter((e) =>
        (e.display_name ?? '').toLowerCase().includes(q) ||
        e.name.toLowerCase().includes(q) ||
        (e.local_path ?? '').toLowerCase().includes(q),
      )
    }
    return list
  })()

  // Tab list — only show type tabs that have at least one engine。
  // 「已加载」不再是顶层 tab(用户 2026-06-11:收进「全部」下的子筛行)。
  const tabs: { id: TabId; label: string; count: number }[] = [
    { id: 'all', label: '全部', count: allEngines.length },
    ...TYPE_ORDER
      .filter((t) => (typeCounts[t] ?? 0) > 0)
      .map((t) => ({ id: t as TabId, label: TAB_LABELS[t] ?? t, count: typeCounts[t] })),
  ]

  return (
    <div
      className="absolute inset-0 overflow-y-auto z-[16]"
      style={{ background: 'var(--bg)' }}
    >
      <div style={{ padding: '24px 28px', maxWidth: 1600, margin: '0 auto' }}>
        {/* m11 header: title + subtitle on left, toolbar on right */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            marginBottom: 18,
          }}
        >
          <div>
            <h1
              style={{
                fontSize: 20,
                fontWeight: 600,
                color: 'var(--text-strong)',
                margin: 0,
              }}
            >
              引擎库
            </h1>
            <p
              style={{
                fontSize: 13,
                color: 'var(--muted)',
                marginTop: 4,
                marginBottom: 0,
              }}
            >
              底层推理引擎文件与 GPU 常驻管理 · 右键卡片查看更多操作
            </p>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
              <Search
                size={13}
                style={{ position: 'absolute', left: 8, color: 'var(--muted)', pointerEvents: 'none' }}
              />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索模型名 / 路径"
                style={{
                  padding: '6px 26px 6px 26px',
                  fontSize: 12,
                  width: 200,
                  borderRadius: 4,
                  border: '1px solid var(--border)',
                  background: 'var(--bg)',
                  color: 'var(--text)',
                  outline: 'none',
                }}
              />
              {search && (
                <button
                  onClick={() => setSearch('')}
                  title="清除"
                  style={{
                    position: 'absolute', right: 6, display: 'flex', alignItems: 'center',
                    background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                    color: 'var(--muted)',
                  }}
                >
                  <X size={13} />
                </button>
              )}
            </div>
            <button
              onClick={() => scanModels.mutate()}
              disabled={scanModels.isPending}
              style={{
                padding: '6px 14px',
                fontSize: 12,
                borderRadius: 4,
                border: '1px solid var(--border)',
                background: 'var(--bg)',
                color: 'var(--text)',
                cursor: scanModels.isPending ? 'wait' : 'pointer',
                opacity: scanModels.isPending ? 0.6 : 1,
              }}
            >
              {scanModels.isPending ? '扫描中...' : '扫描模型'}
            </button>
            {hasAnyMissing && (
              <button
                onClick={() => syncMeta.mutate()}
                disabled={syncMeta.isPending}
                style={{
                  padding: '6px 14px',
                  fontSize: 12,
                  borderRadius: 4,
                  border: 'none',
                  background: 'var(--accent)',
                  color: '#fff',
                  cursor: syncMeta.isPending ? 'wait' : 'pointer',
                  opacity: syncMeta.isPending ? 0.6 : 1,
                }}
              >
                {syncMeta.isPending ? '同步中...' : '+ 拉取模型信息'}
              </button>
            )}
          </div>
        </div>

        {/* m11 tabs row: all / per-type (with non-zero count) / loaded */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            borderBottom: '1px solid var(--border)',
            marginBottom: 18,
            gap: 2,
          }}
        >
          {tabs.map((t) => {
            const isActive = activeTab === t.id
            return (
              <button
                key={t.id}
                onClick={() => { setActiveTab(t.id); setImageBucket('all') }}
                style={{
                  padding: '8px 18px',
                  fontSize: 13,
                  fontWeight: isActive ? 600 : 500,
                  background: 'transparent',
                  border: 'none',
                  borderBottom: '2px solid',
                  borderBottomColor: isActive ? 'var(--accent)' : 'transparent',
                  color: isActive ? 'var(--text)' : 'var(--muted)',
                  cursor: 'pointer',
                  marginBottom: -1,
                }}
              >
                {t.label} {t.count}
              </button>
            )
          })}
        </div>

        {/* 二级子筛:图像 = 全部/已加载 + 文件夹桶;其余 tab(含「全部」)= 全部/已加载。 */}
        {(
          <div style={{ display: 'flex', gap: 6, marginTop: -8, marginBottom: 16, flexWrap: 'wrap' }}>
            {(activeTab === 'image'
              ? imageSubTabs.map((t) => ({ ...t, count: bucketCounts[t.id] ?? 0 }))
              : genericSubTabs
            ).map((t) => {
              const active = imageBucket === t.id
              return (
                <button
                  key={t.id}
                  onClick={() => setImageBucket(t.id)}
                  style={{
                    padding: '3px 12px', fontSize: 11, borderRadius: 12, cursor: 'pointer',
                    background: active ? 'var(--accent-subtle)' : 'var(--bg-hover)',
                    border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                    color: active ? 'var(--accent)' : 'var(--muted)',
                    transition: 'all 0.12s',
                  }}
                >
                  {t.label} {t.count}
                </button>
              )
            })}
          </div>
        )}

        {isLoading && (
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>加载中...</div>
        )}

        {isError && !engines && (
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>
            无法连接后端服务，等待重试...
          </div>
        )}

        {/* m11 single flat grid — 卡片;图像 tab 下按文件夹(diffusion_models/clip/vae/loras)子 tab 过滤。 */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
            gap: 12,
          }}
        >
          {visibleEngines.map((model) => (
            <ModelCard
              key={model.name}
              model={model}
              onContextMenu={(e) => handleContextMenu(e, model)}
              onToggleResident={handleToggleResident}
            />
          ))}
          {/* Bug 3 PR-2c:「已加载」子筛额外列出 runner 里加载的 combo adapter 实体 ——
              工作流动态组装的单文件 combo,不对应注册卡片,故独立渲染。 */}
          {showAdapters &&
            loadedAdapters.map((a) => <AdapterCard key={a.model_id} adapter={a} />)}
        </div>

        {visibleEngines.length === 0 &&
          !(showAdapters && loadedAdapters.length > 0) && !isLoading && (
          <div style={{ fontSize: 11, color: 'var(--muted)', padding: 24, textAlign: 'center' }}>
            {search.trim() ? `没有匹配「${search.trim()}」的模型 · 试试别的关键词` : '该类目没有模型 · 试试别的 tab'}
          </div>
        )}
      </div>

      {ctxMenu.visible && ctxMenu.model && (
        <ContextMenu items={menuItems} position={ctxMenu.position} onClose={closeMenu} />
      )}

      {budgetTarget && (
        <VramBudgetModal engine={budgetTarget} onClose={() => setBudgetTarget(null)} />
      )}

      {paramsTarget && (
        <LaunchParamsModal engine={paramsTarget} onClose={() => setParamsTarget(null)} />
      )}

      {deleteTarget && (
        <DeleteModelDialog engine={deleteTarget} onClose={() => setDeleteTarget(null)} />
      )}
    </div>
  )
}

/** 显存预算弹窗(spec 2026-06-13 PR-2):拉当前设置 + 推荐值,数据就绪后挂载表单。 */
function VramBudgetModal({ engine, onClose }: { engine: EngineInfo; onClose: () => void }) {
  const { data, isLoading } = useVramBudget(engine.name)
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="rounded-lg"
        style={{
          width: 420, maxWidth: '92vw', background: 'var(--bg-elevated, #1a1a1a)',
          border: '1px solid var(--border)', padding: 20,
          display: 'flex', flexDirection: 'column', gap: 14,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>显存预算 · {engine.display_name}</div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)' }}>
            <X size={16} />
          </button>
        </div>
        {isLoading || !data ? (
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>加载中…</div>
        ) : (
          // data 就绪后才挂表单 → 初值用 useState initializer 一次性读取,免 set-state-in-effect。
          <VramBudgetForm name={engine.name} data={data} onClose={onClose} />
        )}
      </div>
    </div>
  )
}

/** 启动参数弹窗(spec 2026-09-22):上下文长度 / 并发 / prefix caching。
 *  显存**不在这里** —— 走隔壁「显存预算…」(绝对 GiB,加载时按实际那张卡换算)。 */
function LaunchParamsModal({ engine, onClose }: { engine: EngineInfo; onClose: () => void }) {
  const { data, isLoading } = useLaunchParams(engine.name)
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="rounded-lg"
        style={{
          width: 460, maxWidth: '92vw', background: 'var(--bg-elevated, #1a1a1a)',
          border: '1px solid var(--border)', padding: 20,
          display: 'flex', flexDirection: 'column', gap: 14,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>启动参数 · {engine.display_name}</div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)' }}>
            <X size={16} />
          </button>
        </div>
        {isLoading || !data ? (
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>加载中…</div>
        ) : data.editable.length === 0 ? (
          /* 该引擎的适配器一个白名单键都不消费(MOSS ASR / TTS 的 __init__ 以 `**kwargs`
             收尾)。渲染编辑器就是给一个点了不管用的按钮:写得进库、GET 报「已覆盖」、
             引擎行为纹丝不动 —— 后端也会 400 拒。2026-09-22 复查 N1。 */
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            该引擎的适配器不消费这些启动参数,没有可调项。
            要调它的启动配置,改它自己的配置文件(models.d 的 yaml / 引擎侧 config)后重新加载。
          </div>
        ) : (
          <>
            {data.overridden.length > 0 && (
              <div style={{ fontSize: 11, color: 'var(--warn)' }}>
                已被运行时覆盖:{data.overridden.join(', ')}(其余为 models.d 的 yaml 默认)
              </div>
            )}
            <LaunchParamsEditor
              engineName={engine.name}
              current={data.effective}
              editable={data.editable}
            />
          </>
        )}
      </div>
    </div>
  )
}

function VramBudgetForm(
  { name, data, onClose }:
  { name: string; data: VramBudgetInfo; onClose: () => void },
) {
  const setBudget = useSetVramBudget()
  const cur = data.current || { mode: 'auto' as VramBudgetMode }
  const [mode, setMode] = useState<VramBudgetMode>(cur.mode)
  const [value, setValue] = useState<string>(() => {
    if (cur.mode === 'percent' && cur.value != null) return String(Math.round(cur.value * 1000) / 10)
    if (cur.mode === 'absolute' && cur.value != null) return String(cur.value)
    return ''
  })

  const card = data.card_total_gb ?? 0
  const recGb = data.recommended_gb ?? null
  const recPct = data.recommended_percent != null ? Math.round(data.recommended_percent * 1000) / 10 : null

  // 实时预览:当前输入将换算成多少 GB / 整卡百分比。
  const num = parseFloat(value)
  let previewGb: number | null = null
  let previewPct: number | null = null
  if (mode === 'percent' && num > 0 && card > 0) { previewPct = num; previewGb = Math.round(card * num) / 1000 * 10 }
  else if (mode === 'absolute' && num > 0 && card > 0) { previewGb = num; previewPct = Math.round(num / card * 1000) / 10 }

  const canSave = mode === 'auto' || (num > 0 && (mode !== 'percent' || num <= 98) && (mode !== 'absolute' || num <= card))

  const onSave = () => {
    if (mode === 'auto') { setBudget.mutate({ name, mode: 'auto' }, { onSuccess: onClose }); return }
    const v = mode === 'percent' ? num / 100 : num  // percent UI 百分数 → 0–1
    setBudget.mutate({ name, mode, value: v }, { onSuccess: onClose })
  }

  return (
    <>
      {recGb != null && (
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          推荐 <b style={{ color: 'var(--text)' }}>{recGb} GB</b>
          {recPct != null && <>（约 {recPct}%）</>}
          {card > 0 && <> · 目标卡共 {card} GB</>}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8 }}>
        {(['auto', 'percent', 'absolute'] as VramBudgetMode[]).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className="rounded-md"
            style={{
              flex: 1, padding: '6px 0', fontSize: 12, cursor: 'pointer',
              border: '1px solid var(--border)',
              background: mode === m ? 'var(--accent-2-subtle)' : 'transparent',
              color: mode === m ? 'var(--accent-2)' : 'var(--text)',
            }}
          >
            {m === 'auto' ? '自动' : m === 'percent' ? '百分比' : '绝对值'}
          </button>
        ))}
      </div>

      {mode === 'auto' ? (
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>按引擎自动公式分配(权重 + 该模态典型 KV/激活余量)。</div>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            type="number"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={mode === 'percent' ? (recPct != null ? String(recPct) : '0–98') : (recGb != null ? String(recGb) : '')}
            style={{
              flex: 1, padding: '6px 8px', fontSize: 13, borderRadius: 6,
              border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)',
            }}
          />
          <span style={{ fontSize: 13, color: 'var(--muted)', width: 28 }}>{mode === 'percent' ? '%' : 'GB'}</span>
        </div>
      )}

      {mode !== 'auto' && previewGb != null && previewPct != null && (
        <div style={{ fontSize: 12, color: 'var(--muted)' }}>
          ≈ {previewGb} GB · 整卡 {previewPct}%
        </div>
      )}

      <div style={{ fontSize: 11, color: 'var(--warning, #d4a017)' }}>需重新加载模型生效(卸载后再加载)。</div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button onClick={onClose} className="rounded-md" style={{ padding: '6px 14px', fontSize: 12, cursor: 'pointer', border: '1px solid var(--border)', background: 'transparent', color: 'var(--text)' }}>取消</button>
        <button
          onClick={onSave}
          disabled={!canSave || setBudget.isPending}
          className="rounded-md"
          style={{
            padding: '6px 14px', fontSize: 12, cursor: canSave ? 'pointer' : 'not-allowed',
            border: 'none', background: 'var(--accent-2)', color: '#fff', opacity: canSave && !setBudget.isPending ? 1 : 0.5,
          }}
        >
          保存
        </button>
      </div>
    </>
  )
}

/** Bug 3 PR-2c:runner 里加载的 combo adapter 实体卡(image/tts)。区别于注册卡 ModelCard
 * —— 它是工作流动态组装的单文件 combo,无 load/resident 操作(临时态,卸载走系统状态的
 * 「释放 image」按钮)。绿色左边框标识已加载。 */
function AdapterCard({ adapter }: { adapter: LoadedAdapter }) {
  const vramGb = adapter.vram_mb != null ? (adapter.vram_mb / 1024).toFixed(1) : null
  const gpuLabel = adapter.gpu_index != null ? `GPU ${adapter.gpu_index}` : null
  const unloadAdapter = useUnloadAdapter()
  return (
    <div
      className="rounded-md"
      title={adapter.source_files.join('\n') || adapter.model_id}
      style={{
        background: 'var(--card)',
        border: '1px solid var(--border)',
        borderLeft: '3px solid var(--accent-2)',
        padding: '10px 12px 10px 10px',
        transition: 'border-color 0.15s ease',
      }}
    >
      <div className="flex items-center gap-2 mb-1">
        <span
          style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-strong)' }}
          className="truncate flex-1"
        >
          {adapter.display_name}
        </span>
        <span style={{ fontSize: 9, color: 'var(--ok)', flexShrink: 0 }}>running</span>
        {/* 已加载 combo 从引擎库直接卸载(统一模型管理收尾 PR-2)。 */}
        <button
          type="button"
          title="卸载（释放显存）"
          onClick={() => unloadAdapter.mutate({ model_id: adapter.model_id })}
          disabled={unloadAdapter.isPending}
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            width: 18, height: 18, borderRadius: 4, flexShrink: 0,
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'var(--muted)', opacity: unloadAdapter.isPending ? 0.4 : 1,
          }}
        >
          <X size={13} />
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-1.5" style={{ fontSize: 9, color: 'var(--muted)' }}>
        <Tag
          color={TYPE_TAG_STYLE[adapter.model_type]?.color ?? 'var(--accent-2)'}
          bg={TYPE_TAG_STYLE[adapter.model_type]?.bg ?? 'var(--accent-2-subtle)'}
        >
          {TYPE_LABELS[adapter.model_type]?.split(' ')[0] ?? adapter.model_type}
        </Tag>
        {adapter.pipeline_class && <Tag>{adapter.pipeline_class}</Tag>}
        {gpuLabel && <span>{gpuLabel}</span>}
        {vramGb && <span>· {vramGb} GB</span>}
        {adapter.last_used_ago_sec != null && (
          <span>· 上次用 {Math.round(adapter.last_used_ago_sec)}s 前</span>
        )}
      </div>
    </div>
  )
}

function ModelCard({
  model,
  onContextMenu,
  onToggleResident,
}: {
  model: EngineInfo
  onContextMenu: (e: React.MouseEvent) => void
  onToggleResident: (engine: EngineInfo) => void
}) {
  const notDownloaded = model.local_path != null && !model.local_exists
  // m11 .loaded — 3px green left border, padding adjusted to keep total 1px+12px
  const isLoaded = model.status === 'loaded'

  return (
    <div
      className="rounded-md"
      onContextMenu={onContextMenu}
      style={{
        background: 'var(--card)',
        border: '1px solid var(--border)',
        borderLeft: isLoaded ? '3px solid var(--accent-2)' : '1px solid var(--border)',
        padding: isLoaded ? '10px 12px 10px 10px' : '10px 12px',
        opacity: notDownloaded ? 0.6 : 1,
        cursor: 'context-menu',
        transition: 'border-color 0.15s ease',
      }}
    >
      {/* Row 1: Name full-width — 不再 truncate(末尾省略号会切掉 bf16/fp8mixed 这种精度后缀,
          导致一堆 Flux2-Klein-9B-True-v… 分不清谁是谁)。改 2 行 clamp + break-all,长名也能
          看到关键后缀;hover 看全名。徽标/状态行下移(Row 1b),让名字独占整宽。 */}
      <div className="flex items-start gap-2 mb-1">
        <span
          title={`${model.organization ? model.organization + '/' : ''}${model.display_name}`}
          style={{
            fontSize: 12, fontWeight: 600, color: 'var(--text-strong)',
            wordBreak: 'break-all', lineHeight: 1.3,
            display: '-webkit-box', WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical', overflow: 'hidden',
          }}
          className="flex-1"
        >
          {model.organization ? `${model.organization}/` : ''}
          {model.display_name}
        </span>
        <CopyButton text={model.name} />
      </div>

      {/* Row 1b: 徽标 + 状态(从名字行下移)。状态徽标靠右(marginLeft auto)。 */}
      <div className="flex items-center flex-wrap gap-1.5 mb-1">
        {model.auto_detected && (
          <span
            style={{
              fontSize: 8,
              padding: '1px 5px',
              borderRadius: 3,
              background: 'color-mix(in srgb, var(--accent-2) 18%, transparent)',
              color: 'var(--accent-2)',
              flexShrink: 0,
            }}
          >
            自动检测
          </span>
        )}
        {/* 统一引擎库 kind 徽标:超分(SeedVR2,可加载)/ 组件 / LoRA(随 pipeline 加载,不独立加载)。 */}
        {model.kind && model.kind !== 'model' && (
          <span
            title={
              model.kind === 'upscale'
                ? 'SeedVR2 超分(by-key 可独立加载)'
                : '单文件组件，随图像 pipeline 加载，不能独立加载'
            }
            style={{
              fontSize: 8, padding: '1px 5px', borderRadius: 3, flexShrink: 0,
              background: 'color-mix(in srgb, var(--accent) 16%, transparent)',
              color: 'var(--accent)',
            }}
          >
            {model.kind === 'upscale' ? '超分' : model.kind === 'lora' ? 'LoRA' : '组件'}
          </span>
        )}
        {/* 红色「未注册」只给真·无 adapter 的整模型(如 ERNIE-Image);组件/LoRA/超分有自己的徽标。 */}
        {!model.has_adapter && (!model.kind || model.kind === 'model') && (
          <span
            title="adapter 未实现，无法加载。需先在 backend/configs/models.yaml 添加 adapter 字段。"
            style={{
              fontSize: 8,
              padding: '1px 5px',
              borderRadius: 3,
              background: 'rgba(239,68,68,0.14)',
              color: 'var(--error, #ef4444)',
              flexShrink: 0,
            }}
          >
            未注册
          </span>
        )}
        <span style={{ marginLeft: 'auto', flexShrink: 0 }}>
          <StatusBadge
            status={model.status}
            loadedGpus={model.loaded_gpus}
            modelName={model.name}
            detail={model.status_detail}
          />
        </span>
      </div>

      {/* Row 2: Tags line */}
      <div className="flex flex-wrap items-center gap-1.5" style={{ fontSize: 9, color: 'var(--muted)' }}>
        <Tag
          color={TYPE_TAG_STYLE[model.type]?.color ?? 'var(--accent-2)'}
          bg={TYPE_TAG_STYLE[model.type]?.bg ?? 'var(--accent-2-subtle)'}
        >
          {TYPE_LABELS[model.type]?.split(' ')[0] ?? model.type}
        </Tag>
        {model.model_size && <Tag icon="📦">{model.model_size}</Tag>}
        {/* image engines: surface LoRA count so operator can verify the
         scanner is finding their files without leaving the page. */}
        {model.type === 'image' && model.lora_count !== null && (
          <Tag color="var(--info)">{model.lora_count} LoRA</Tag>
        )}
        {model.frameworks?.map((f) => (
          <Tag key={f} icon="⚙">{f}</Tag>
        ))}
        {model.license && <Tag icon="📄">{model.license}</Tag>}
        {model.languages && model.languages.length > 0 && (
          model.languages.length <= 3
            ? model.languages.map((l) => <Tag key={l}>{l.toUpperCase()}</Tag>)
            : <Tag>{model.languages.length} languages</Tag>
        )}
        {model.tags?.slice(0, 3).map((t) => (
          <span key={t} style={{ color: 'var(--muted)' }}>• {t}</span>
        ))}
      </div>

      {/* Row 3: Local info (read-only) */}
      {/* Row 3a: chips (VRAM / GPU / resident toggle) — m11 style */}
      <div className="flex items-center gap-3 mt-1" style={{ fontSize: 9, color: 'var(--muted)' }}>
        <span>{model.vram_gb}GB VRAM</span>
        {/* 已加载 → 显示实际落卡(loaded_gpus,物理 index,与 Dashboard 一致);
         未加载 → 显示配置槽位并标注「配置」,避免与 Dashboard 的实际卡名读成矛盾。 */}
        {model.loaded_gpus && model.loaded_gpus.length > 0 ? (
          <span title="实际加载所在 GPU(物理编号,与 Dashboard 一致)">
            GPU {model.loaded_gpus.join('+')}
          </span>
        ) : (
          <span title="配置的 GPU 槽位(未加载时的预定落卡,实际以加载时分配为准)">
            GPU {Array.isArray(model.gpu) ? model.gpu.join('+') : model.gpu}(配置)
          </span>
        )}
        <button
          title={model.resident ? '常驻(不被自动卸载) — 点击取消常驻' : '按需(空闲自动卸载) — 点击设为常驻'}
          onClick={(e) => {
            e.stopPropagation()
            onToggleResident(model)
          }}
          style={{
            color: model.resident ? 'var(--warn)' : 'var(--muted)',
            background: model.resident
              ? 'color-mix(in srgb, var(--warn) 15%, transparent)'
              : 'var(--bg)',
            padding: '1px 5px',
            borderRadius: 3,
            border: 'none',
            cursor: 'pointer',
            fontSize: 9,
            display: 'inline-flex',
            alignItems: 'center',
          }}
        >
          {model.resident ? <Pin size={11} /> : <PinOff size={11} />}
        </button>
      </div>

      {/* Row 3b: path on its own line, full width — m11 has `.src { flex: 1 1 100% }` */}
      {model.local_path && (
        <div
          className="mt-1"
          style={{
            fontSize: 9,
            color: model.local_exists ? 'var(--muted-strong, var(--muted))' : 'var(--warn)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            fontFamily: 'monospace',
          }}
          title={model.local_path}
        >
          <span style={{ color: model.local_exists ? 'var(--accent-2)' : 'var(--warn)', marginRight: 4 }}>
            {model.local_exists ? '✓' : '✗'}
          </span>
          {model.local_path}
        </div>
      )}
    </div>
  )
}

function Tag({
  children,
  icon,
  color,
  bg,
}: {
  children: React.ReactNode
  icon?: string
  color?: string
  bg?: string
}) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 2,
        padding: '1px 5px',
        borderRadius: 3,
        background: bg ?? (color ? `color-mix(in srgb, ${color} 12%, transparent)` : 'var(--bg)'),
        color: color ?? 'var(--muted)',
        fontSize: 9,
        whiteSpace: 'nowrap',
      }}
    >
      {icon && <span>{icon}</span>}
      {children}
    </span>
  )
}

// Per-model loading start times (module-scoped so they survive remounts within session)
const _loadingStartedAt: Map<string, number> = new Map()

function StatusBadge({
  status,
  loadedGpus,
  modelName,
  detail,
}: {
  status: string
  loadedGpus?: number[] | null
  modelName?: string
  detail?: string | null
}) {
  const [, force] = useState(0)

  // Track when loading/installing started; tick every second to refresh elapsed
  useEffect(() => {
    if (!modelName) return
    if (status === 'loading' || status === 'installing') {
      if (!_loadingStartedAt.has(modelName)) {
        _loadingStartedAt.set(modelName, Date.now())
      }
      const id = setInterval(() => force((n) => n + 1), 1000)
      return () => clearInterval(id)
    }
    _loadingStartedAt.delete(modelName)
  }, [status, modelName])

  const gpuLabel = loadedGpus && loadedGpus.length > 0
    ? ` · GPU ${loadedGpus.join('+')}`
    : ''

  let elapsedLabel = 'loading...'
  if ((status === 'loading' || status === 'installing') && modelName) {
    const startedAt = _loadingStartedAt.get(modelName)
    if (startedAt) {
      const s = Math.floor((Date.now() - startedAt) / 1000)
      const verb = status === 'installing' ? 'installing' : 'loading'
      elapsedLabel = s < 60 ? `${verb} ${s}s` : `${verb} ${Math.floor(s / 60)}m${s % 60}s`
    } else {
      elapsedLabel = status === 'installing' ? 'installing...' : 'loading...'
    }
  }

  const config: Record<string, { color: string; label: string; animate?: boolean }> = {
    loaded:         { color: 'var(--ok)',           label: `running${gpuLabel}` },
    loading:        { color: 'var(--warn)',         label: elapsedLabel, animate: true },
    failed:         { color: 'var(--accent)',       label: 'failed' },
    installing:     { color: 'var(--warn)',         label: elapsedLabel, animate: true },
    installed:      { color: 'var(--muted-strong)', label: 'installed' },
    install_failed: { color: 'var(--accent)',       label: 'install failed' },
    unloaded:       { color: 'var(--muted-strong)', label: 'idle' },
  }
  const { color, label, animate } = config[status] ?? config.unloaded

  const tooltip = detail || ({
    failed: 'load failed',
    loading: '正在加载',
    installing: '正在安装依赖',
    install_failed: 'dep install failed',
  } as Record<string, string>)[status]

  return (
    <span
      className="flex items-center gap-1"
      style={{ fontSize: 9, color, flexShrink: 0, cursor: tooltip ? 'help' : 'default' }}
      title={tooltip}
    >
      <span
        className="inline-block rounded-full"
        style={{
          width: 6,
          height: 6,
          background: color,
          ...(animate ? { animation: 'loading-pulse 1.5s ease-in-out infinite' } : {}),
        }}
      />
      {label}
    </span>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      title={`复制: ${text}`}
      onClick={(e) => {
        e.stopPropagation()
        navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      }}
      style={{
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        padding: 2,
        color: copied ? 'var(--ok)' : 'var(--muted)',
        flexShrink: 0,
        display: 'flex',
        alignItems: 'center',
      }}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  )
}
