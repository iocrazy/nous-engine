import { create } from 'zustand'

export type OverlayId =
  | 'dashboard'
  | 'training'
  | 'models'           // 引擎库
  | 'settings'
  | 'api-keys-list'    // m10 v3 API Key 列表
  | 'api-key-detail'   // m10 详情（id 从 URL 取）
  | 'agents'           // legacy — kept addressable for now, hidden in v3 rail
  | 'logs'
  | 'node-packages'    // legacy — moved into Settings sub-page in v3
  | 'services'         // v3 服务列表
  | 'apps'             // legacy alias of services; kept so old links/state don't 404
  | 'service-detail'   // v3 服务详情（id 从 URL 取）
  | 'usage'            // v3 新
  | 'history'          // 历史出图画廊(借鉴 Infinite-Canvas history;拉 image 类 task)
  | 'status'           // 系统状态页(组件健康 + 7 天 uptime,对齐 status.claude.ai)

const ROUTE_TO_OVERLAY: Record<string, OverlayId> = {
  '/models': 'models',
  '/services': 'services',
  '/apps': 'apps',
  '/agents': 'agents',
  '/settings': 'settings',
  '/dashboard': 'dashboard',
  '/training': 'training',
  '/api-keys': 'api-keys-list',
  '/logs': 'logs',
  '/node-packages': 'node-packages',
  '/usage': 'usage',
  '/gallery': 'history',
  '/history': 'history',
  '/status': 'status',
}

export function overlayForPath(pathname: string): OverlayId | null {
  const path = pathname.replace(/\/+$/, '').toLowerCase()
  if (path.startsWith('/services/')) return 'service-detail'
  if (path.startsWith('/api-keys/')) return 'api-key-detail'
  return ROUTE_TO_OVERLAY[path] ?? null
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
  taskPanelMode: TaskPanelMode
  /** 保留哪些状态;默认全 5 个。空集 = 啥都不显示(故意的,UI 给「全选」按钮恢复)。 */
  taskFilterStatuses: ReadonlySet<TaskStatus>
  taskSortKey: TaskSortKey
  taskSortDir: TaskSortDir
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

export const usePanelStore = create<PanelState>((set) => ({
  taskPanelMode: _initialTaskPanelMode,
  taskFilterStatuses: _loadFilterStatuses(),
  taskSortKey: _initialSort.key,
  taskSortDir: _initialSort.dir,


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
