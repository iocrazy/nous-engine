import { create } from 'zustand'

export type PanelId = 'nodes' | 'workflows' | 'presets'
export type OverlayId =
  | 'dashboard'
  | 'models'           // 引擎库
  | 'settings'
  | 'preset-detail'
  | 'api-keys-list'    // m10 v3 API Key 列表
  | 'api-key-detail'   // m10 详情（id 从 URL 取）
  | 'agents'           // legacy — kept addressable for now, hidden in v3 rail
  | 'logs'
  | 'node-packages'    // legacy — moved into Settings sub-page in v3
  | 'services'         // v3 服务列表
  | 'apps'             // legacy alias of services; kept so old links/state don't 404
  | 'service-detail'   // v3 服务详情（id 从 URL 取）
  | 'workflows-list'   // v3 m08 列表（vs canvas at /workflows/:id）
  | 'usage'            // v3 新
  | 'history'          // 历史出图画廊(借鉴 Infinite-Canvas history;拉 image 类 task)
  | 'status'           // 系统状态页(组件健康 + 7 天 uptime,对齐 status.claude.ai)

/** URL path → overlay。App 的 RouteSync 和本 store 的初值共用这一份映射:
 *  少一处漏改就少一次「刷新后 overlay 慢一拍」。 */
const ROUTE_TO_OVERLAY: Record<string, OverlayId> = {
  '/models': 'models',
  '/services': 'services',
  '/apps': 'apps',
  '/agents': 'agents',
  '/settings': 'settings',
  '/dashboard': 'dashboard',
  '/api-keys': 'api-keys-list',
  '/logs': 'logs',
  '/node-packages': 'node-packages',
  '/usage': 'usage',
  '/history': 'history',
  '/status': 'status',
}

/** 纯函数:给定 pathname 得到应显示的 overlay(null = 工作流画布)。 */
export function overlayForPath(pathname: string): OverlayId | null {
  // `/workflows/:id` 是画布编辑器(无 overlay);`/workflows`(无 id)是 v3 m08 列表页。
  if (pathname === '/workflows') return 'workflows-list'
  if (pathname.startsWith('/workflows/')) return null
  // `/services/:id`、`/api-keys/:id` 详情页各走独立 overlay slot,
  // NodeEditor 据此决定挂哪个视图。
  if (pathname.startsWith('/services/')) return 'service-detail'
  if (pathname.startsWith('/api-keys/')) return 'api-key-detail'
  return ROUTE_TO_OVERLAY[pathname] ?? null
}

/** TaskPanel 模式:dock=右侧抽屉(全高,modal-ish),float=右下角浮窗(可拖拽,不阻塞操作)。
 *  对齐 ComfyUI「停靠 / 悬浮」两态。localStorage 持久。 */
export type TaskPanelMode = 'dock' | 'float'

/** TaskPanel 状态筛选 + 排序(Linear/Vercel 风任务管理面板,localStorage 持久)。 */
export type TaskStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
export const ALL_TASK_STATUSES: readonly TaskStatus[] = [
  'queued', 'running', 'completed', 'failed', 'cancelled',
] as const
export type TaskSortKey = 'created' | 'duration'
export type TaskSortDir = 'asc' | 'desc'

interface PanelState {
  activePanel: PanelId | null
  activeOverlay: OverlayId | null
  selectedPresetId: string | null
  panelWidth: number
  taskPanelMode: TaskPanelMode
  /** 保留哪些状态;默认全 5 个。空集 = 啥都不显示(故意的,UI 给「全选」按钮恢复)。 */
  taskFilterStatuses: ReadonlySet<TaskStatus>
  taskSortKey: TaskSortKey
  taskSortDir: TaskSortDir
  setPanel: (id: PanelId | null) => void
  togglePanel: (id: PanelId) => void
  setOverlay: (id: OverlayId | null) => void
  toggleOverlay: (id: OverlayId) => void
  openPresetDetail: (presetId: string) => void
  setPanelWidth: (width: number) => void
  setTaskPanelMode: (mode: TaskPanelMode) => void
  setTaskFilterStatuses: (statuses: ReadonlySet<TaskStatus>) => void
  setTaskSort: (key: TaskSortKey, dir: TaskSortDir) => void
}

const TASK_PANEL_MODE_KEY = 'nous.taskPanel.mode'
const TASK_FILTER_KEY = 'nous.taskPanel.filterStatuses'
const TASK_SORT_KEY = 'nous.taskPanel.sort'

const _initialTaskPanelMode: TaskPanelMode =
  typeof localStorage !== 'undefined' && localStorage.getItem(TASK_PANEL_MODE_KEY) === 'float'
    ? 'float'
    : 'dock'

function _loadFilterStatuses(): ReadonlySet<TaskStatus> {
  if (typeof localStorage === 'undefined') return new Set(ALL_TASK_STATUSES)
  const raw = localStorage.getItem(TASK_FILTER_KEY)
  if (!raw) return new Set(ALL_TASK_STATUSES)
  try {
    const arr = JSON.parse(raw) as unknown
    if (!Array.isArray(arr)) return new Set(ALL_TASK_STATUSES)
    const valid = arr.filter((s): s is TaskStatus =>
      ALL_TASK_STATUSES.includes(s as TaskStatus))
    return new Set(valid)
  } catch {
    return new Set(ALL_TASK_STATUSES)
  }
}

function _loadSort(): { key: TaskSortKey; dir: TaskSortDir } {
  if (typeof localStorage === 'undefined') return { key: 'created', dir: 'desc' }
  const raw = localStorage.getItem(TASK_SORT_KEY)
  if (!raw) return { key: 'created', dir: 'desc' }
  try {
    const v = JSON.parse(raw) as { key?: unknown; dir?: unknown }
    const key: TaskSortKey = v.key === 'duration' ? 'duration' : 'created'
    const dir: TaskSortDir = v.dir === 'asc' ? 'asc' : 'desc'
    return { key, dir }
  } catch {
    return { key: 'created', dir: 'desc' }
  }
}

const _initialSort = _loadSort()

// overlay 初值直接从当前 URL 推,而不是等 RouteSync 的 effect 回填:硬刷新
// `/services` 这类页面时,NodeEditor 的第一次 render 就已经知道要挂 overlay,
// 画布/画布浮动条根本不会先挂一轮再被换掉。
const _initialOverlay: OverlayId | null =
  typeof window !== 'undefined' ? overlayForPath(window.location.pathname) : null

export const usePanelStore = create<PanelState>((set, get) => ({
  activePanel: 'nodes',
  activeOverlay: _initialOverlay,
  selectedPresetId: null,
  panelWidth: 260,
  taskPanelMode: _initialTaskPanelMode,
  taskFilterStatuses: _loadFilterStatuses(),
  taskSortKey: _initialSort.key,
  taskSortDir: _initialSort.dir,

  setPanel: (id) => set({ activePanel: id, activeOverlay: null }),

  togglePanel: (id) => {
    const { activePanel } = get()
    set({
      activePanel: activePanel === id ? null : id,
      activeOverlay: null,
    })
  },

  setOverlay: (id) => set({ activeOverlay: id, activePanel: null }),

  toggleOverlay: (id) => {
    const { activeOverlay } = get()
    set({
      activeOverlay: activeOverlay === id ? null : id,
      activePanel: null,
    })
  },

  openPresetDetail: (presetId) =>
    set({ activeOverlay: 'preset-detail', activePanel: null, selectedPresetId: presetId }),

  setPanelWidth: (width) => set({ panelWidth: Math.max(200, Math.min(400, width)) }),

  setTaskPanelMode: (mode) => {
    if (typeof localStorage !== 'undefined') localStorage.setItem(TASK_PANEL_MODE_KEY, mode)
    set({ taskPanelMode: mode })
  },

  setTaskFilterStatuses: (statuses) => {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(TASK_FILTER_KEY, JSON.stringify(Array.from(statuses)))
    }
    set({ taskFilterStatuses: statuses })
  },

  setTaskSort: (key, dir) => {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(TASK_SORT_KEY, JSON.stringify({ key, dir }))
    }
    set({ taskSortKey: key, taskSortDir: dir })
  },
}))
