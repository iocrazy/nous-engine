/**
 * GlobalTopbar — 全局顶部 nav(PR-3a 任务面板重置)。
 *
 * D10 决策修正:task UI 是 **GlobalTopbar 下拉悬浮面板**(不是右侧固定 sidebar)。
 * 下拉里复刻 mockup `variant-final.html` 的完整 task 结构:Active/History tabs +
 * cook overview(运行/排队/完成 + GPU)+ 任务行(4 type 配色)。
 *
 * Topbar 布局:
 *   nous-logo  · spacer · infra 状态(/health)· ⌕ search · ☰ tasks(下拉)· ⋮ user
 */
import { useState, useRef, useEffect, useMemo } from 'react'
import { Search, MoreVertical, LogOut, ListTodo, Cpu, Menu, Info } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAdminLogout, useAdminMe } from '../../api/admin'
import { useTasks, type ExecutionTask } from '../../api/tasks'
import { useGpuStats, pickPrimaryGpu, type GpuInfo } from '../../api/gpuStats'
import { useHealth, healthDegradedReasons } from '../../api/health'
import { useExecutionStore } from '../../stores/execution'
import ActiveTaskRow from './ActiveTaskRow'
import HistoryCard from './HistoryCard'
import TopbarGpuMonitor from './TopbarGpuMonitor'

export default function GlobalTopbar() {
  const navigate = useNavigate()
  const me = useAdminMe()
  const logout = useAdminLogout()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  const canLogout = me.data?.login_required && me.data?.authenticated

  // 点外部关菜单
  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [menuOpen])

  const handleLogout = () => {
    logout.mutate(undefined, {
      onSuccess: () => {
        setMenuOpen(false)
        navigate('/')
      },
    })
  }

  return (
    <div
      className="flex items-center px-4 shrink-0 z-30"
      style={{
        height: 44,
        background: 'var(--tp-bg-panel)',
        borderBottom: '1px solid var(--tp-border-faint)',
      }}
    >
      {/* Logo */}
      <div className="flex items-center gap-2 font-bold text-[15px]" style={{ color: 'var(--tp-text)' }}>
        <span
          style={{
            width: 8, height: 8, borderRadius: 2,
            background: 'linear-gradient(135deg, var(--type-image), var(--type-tts) 50%, var(--type-llm))',
          }}
        />
        nous
      </div>

      <div className="flex-1" />

      {/* 硬件监控条(CPU/内存/GPU)—— 放在 healthy 左侧,ComfyUI 式 */}
      <TopbarGpuMonitor />
      <div style={{ width: 1, height: 16, background: 'var(--tp-border-faint)', marginRight: 12 }} />

      {/* 全局状态 */}
      <InfraStatus />

      {/* 搜索 placeholder */}
      <button
        className="p-1.5 transition-colors"
        style={{ color: 'var(--tp-text-muted)', cursor: 'pointer' }}
        aria-label="搜索"
        title="搜索(占位)"
      >
        <Search size={16} />
      </button>

      {/* PR-3a:任务下拉悬浮面板(对齐 mockup variant-final sidebar 形态)*/}
      <TaskMenu />

      {/* user 菜单(⋮)—— 只放退出登录。 */}
      {canLogout && (
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen((o) => !o)}
            className="p-1.5 ml-1 transition-colors"
            style={{ color: 'var(--tp-text-muted)', cursor: 'pointer' }}
            aria-label="用户菜单"
            aria-expanded={menuOpen}
            aria-haspopup="menu"
          >
            <MoreVertical size={16} />
          </button>
          {menuOpen && (
            <div
              role="menu"
              className="absolute right-0 top-full mt-2 py-1 rounded-md"
              style={{
                minWidth: 160,
                background: 'var(--tp-bg-card)',
                border: '1px solid var(--tp-border-strong)',
                boxShadow: 'var(--shadow-card, 0 6px 16px rgba(0,0,0,0.3))',
              }}
            >
              <button
                onClick={handleLogout}
                role="menuitem"
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs transition-colors hover:bg-[var(--tp-bg-hover)]"
                style={{ color: 'var(--tp-text)', cursor: 'pointer' }}
              >
                <LogOut size={14} style={{ color: 'var(--tp-text-muted)' }} />
                退出登录
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * InfraStatus — 顶栏「infra healthy」,读 /health(useHealth 20s 轮询)。
 *
 * 2026-09-26 前这里是写死的文字:ComfyUI 被换成手工实例停了一天多,顶栏一直绿着。
 * 拉不到 /health → 红 down;status=degraded → 琥珀 degraded(title 列原因);否则绿 healthy。
 */
function InfraStatus() {
  const health = useHealth()
  const h = health.data
  let color = 'var(--tp-text-dim)'
  let label = 'checking'
  let title = '正在检查 /health'
  if (health.isError) {
    color = 'var(--status-failed)'
    label = 'down'
    title = '拉不到 /health —— 后端可能挂了'
  } else if (h?.status === 'degraded') {
    color = 'var(--status-queued)'
    label = 'degraded'
    const reasons = healthDegradedReasons(h)
    title = reasons.length ? reasons.join('\n') : 'degraded'
  } else if (h) {
    color = 'var(--status-running)'
    label = 'healthy'
    title = 'infra healthy'
  }
  return (
    <div
      className="text-xs flex items-center gap-1.5 mr-4"
      style={{ color: 'var(--tp-text-muted)' }}
      title={title}
      data-testid="infra-status"
    >
      <span
        style={{
          width: 7, height: 7, borderRadius: '50%',
          background: color,
          boxShadow: `0 0 8px ${color}`,
        }}
      />
      infra <span style={{ color: 'var(--tp-text)', fontWeight: 500 }}>{label}</span>
    </div>
  )
}

/* ============================================================
 * TaskMenu — 任务下拉悬浮面板(PR-3a)
 *
 * 入口:Topbar 右侧 ListTodo 图标 + 活动任务 badge。
 * 下拉内容(复刻 mockup variant-final sidebar 内部结构,但作为悬浮 panel):
 *   ┌─ sb-header:全部任务 [global queue]
 *   ├─ sb-tabs:Active N · History N
 *   ├─ cook-overview:●N 运行 · ●N 排队 · ●N 完成 · GPU
 *   ├─ sb-body:Active tab → 任务行(timeline node + task-card + L3 callout)
 *   │           History tab → 4 type 任务行(image/tts/llm/vision 配色)
 *   └─ footer:打开完整任务面板 →
 * ============================================================ */

const ACTIVE_STATUSES = new Set(['queued', 'running'])
// type 标签 / 状态色 / getTaskType helper 已搬到子组件(ActiveTaskRow / HistoryCard)各自维护。

function TaskMenu() {
  const { data: tasks } = useTasks()
  const togglePanel = useExecutionStore((s) => s.toggleTaskPanel)
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'active' | 'history'>('active')
  const wrapRef = useRef<HTMLDivElement>(null)

  const counts = useMemo(() => {
    const all = tasks ?? []
    const running = all.filter((t) => t.status === 'running').length
    const queued = all.filter((t) => t.status === 'queued').length
    const done = all.filter((t) => t.status === 'completed').length
    const failed = all.filter((t) => t.status === 'failed').length
    const cancelled = all.filter((t) => t.status === 'cancelled').length
    const active = running + queued
    const history = all.length - active
    return { running, queued, done, failed, cancelled, active, history }
  }, [tasks])

  // section label:有 failed 时显式拆分,让用户从标题就看到错误数量(PR-9)。
  const buildHistoryLabel = (visible: number): string => {
    const parts = [`最近 ${visible} 条`]
    if (counts.failed > 0) parts.push(`失败 ${counts.failed}`)
    if (counts.cancelled > 0) parts.push(`取消 ${counts.cancelled}`)
    return parts.join(' · ')
  }

  const { activeTasks, historyTasks } = useMemo(() => {
    const all = tasks ?? []
    return {
      activeTasks: all.filter((t) => ACTIVE_STATUSES.has(t.status)),
      historyTasks: all.filter((t) => !ACTIVE_STATUSES.has(t.status)),
    }
  }, [tasks])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const badge = counts.active

  return (
    <div className="relative ml-1" ref={wrapRef}>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-label={badge > 0 ? `${badge} 个活动任务` : '任务'}
        aria-expanded={open}
        aria-haspopup="menu"
        className="p-1.5 transition-colors relative"
        style={{
          color: badge > 0 ? 'var(--status-running)' : 'var(--tp-text-muted)',
          cursor: 'pointer',
        }}
      >
        <ListTodo size={16} />
        {badge > 0 && (
          <span
            className="absolute -top-0.5 -right-0.5 flex items-center justify-center text-[9px] font-semibold rounded-full"
            style={{
              minWidth: 14, height: 14, padding: '0 3px',
              background: 'var(--status-running)', color: '#0a0a0c',
            }}
          >
            {badge > 99 ? '99+' : badge}
          </span>
        )}
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-2 rounded-md overflow-hidden flex flex-col"
          style={{
            width: 400, maxHeight: 720,
            background: 'var(--tp-bg-panel)',
            border: '1px solid var(--tp-border-strong)',
            boxShadow: 'var(--shadow-card, 0 12px 32px rgba(0,0,0,0.5))',
          }}
        >
          {/* sb-header(对齐 mockup:左 ☰ menu icon + 标题 + global queue 标签)*/}
          <div
            className="flex items-center gap-2 shrink-0"
            style={{ height: 44, padding: '0 16px', borderBottom: '1px solid var(--tp-border-faint)' }}
          >
            <Menu size={16} style={{ color: 'var(--tp-text-muted)' }} />
            <span className="text-sm font-semibold" style={{ color: 'var(--tp-text)' }}>
              全部任务
            </span>
            <span
              className="text-[10px] font-medium px-1.5 py-0.5 rounded font-mono"
              style={{
                background: 'var(--tp-bg-elevated)',
                color: 'var(--tp-text-muted)',
              }}
            >
              global queue
            </span>
          </div>

          {/* sb-tabs */}
          <div
            className="flex shrink-0"
            style={{ padding: '0 16px', gap: 18, borderBottom: '1px solid var(--tp-border-faint)' }}
          >
            <SbTab label="Active" count={counts.active} active={tab === 'active'} onClick={() => setTab('active')} />
            <SbTab label="History" count={counts.history} active={tab === 'history'} onClick={() => setTab('history')} />
          </div>

          {/* cook-overview */}
          <div
            className="shrink-0"
            style={{
              padding: '10px 16px',
              background: 'linear-gradient(180deg, var(--tp-bg-card-subtle, #0f0f12) 0%, var(--tp-bg-panel) 100%)',
              borderBottom: '1px solid var(--tp-border-faint)',
            }}
          >
            <div className="flex items-center gap-3.5 text-[11.5px]">
              <CookStat dotColor="var(--status-running)" glow num={counts.running} label="运行" />
              <CookStat dotColor="var(--status-queued)" num={counts.queued} label="排队" />
              <CookStat dotColor="var(--tp-text-dim)" num={counts.done} label="完成" />
              <GpuInfoChip />
            </div>
          </div>

          {/* sb-body —— PR-5 对齐 mockup:Active tab 同时显示 active + recent,
              不让用户切 tab 才看到 history。 */}
          <div className="flex-1 overflow-y-auto">
            {tab === 'active' ? (
              <>
                <TaskList
                  tasks={activeTasks}
                  emptyText="当前无活动任务"
                  sectionLabel={`正在跑 · ${counts.active} 个`}
                  mode="active"
                />
                {historyTasks.length > 0 && (
                  <TaskList
                    tasks={historyTasks.slice(0, 5)}
                    emptyText=""
                    sectionLabel={buildHistoryLabel(Math.min(5, counts.history))}
                    mode="history"
                  />
                )}
              </>
            ) : (
              <TaskList
                tasks={historyTasks}
                emptyText="暂无历史记录"
                sectionLabel={buildHistoryLabel(counts.history)}
                mode="history"
              />
            )}
          </div>

          {/* footer:对齐 mockup `ⓘ collapsed → 点击展开 → 缩略图点开 modal` 解释性
              + 「打开完整任务面板 →」操作链接(放右侧)。 */}
          <div
            className="shrink-0 flex items-center gap-2 px-3 py-2 text-[10.5px]"
            style={{ borderTop: '1px solid var(--tp-border-faint)', color: 'var(--tp-text-faint)' }}
          >
            <Info size={11} />
            <span>点击展开 · 缩略图点开大图</span>
            <button
              onClick={() => { setOpen(false); togglePanel() }}
              role="menuitem"
              className="ml-auto transition-colors hover:text-[var(--tp-text)]"
              style={{ color: 'var(--tp-text-muted)', cursor: 'pointer' }}
            >
              完整面板 →
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function SbTab({
  label, count, active, onClick,
}: {
  label: string
  count: number
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1.5 text-xs font-medium transition-colors"
      style={{
        height: 36,
        color: active ? 'var(--tp-text)' : 'var(--tp-text-muted)',
        borderBottom: `2px solid ${active ? 'var(--status-running)' : 'transparent'}`,
        cursor: 'pointer',
      }}
      aria-current={active ? 'page' : undefined}
    >
      {label}
      <span
        className="text-[10px] px-1.5 py-0.5 rounded font-mono"
        style={{
          background: active ? 'rgba(74, 222, 128, 0.13)' : 'var(--tp-bg-elevated)',
          color: active ? 'var(--status-running)' : 'var(--tp-text-muted)',
        }}
      >
        {count}
      </span>
    </button>
  )
}

/**
 * GpuInfoChip — cook overview 右侧 GPU 利用率显示(对齐 mockup「⚙️ GPU 1·62%」)。
 *
 * 显示主卡(默认 cuda:1)的 utilization_gpu 实时百分比 + 利用率热度色:
 * - 0-30%   灰(idle)
 * - 30-70%  绿(working)
 * - 70-100% 黄→红(saturated)
 *
 * 数据来自 useGpuStats(2s 轮询 /api/v1/monitor/stats)。
 */
function GpuInfoChip() {
  const { data: gpus } = useGpuStats()
  const gpu = pickPrimaryGpu(gpus)
  return (
    <span
      className="ml-auto inline-flex items-center gap-1.5 text-[11px] font-mono"
      style={{ color: 'var(--tp-text-muted)' }}
      title={gpu ? `${gpu.name} · mem ${(gpu.memory_used_mb / 1024).toFixed(1)}/${(gpu.memory_total_mb / 1024).toFixed(0)} GiB · ${gpu.temperature}°C · ${gpu.power_draw_w.toFixed(0)}W` : 'GPU stats 加载中...'}
    >
      <Cpu size={12} style={{ color: gpuColor(gpu) }} />
      {gpu ? (
        <>
          GPU {gpu.index}·<span style={{ color: 'var(--tp-text)', fontWeight: 600 }}>{gpu.utilization_gpu}%</span>
        </>
      ) : (
        'GPU'
      )}
    </span>
  )
}

function gpuColor(gpu: GpuInfo | null): string {
  if (!gpu) return 'var(--tp-text-muted)'
  const u = gpu.utilization_gpu
  if (u < 30) return 'var(--tp-text-muted)'
  if (u < 70) return 'var(--status-running)'
  if (u < 90) return 'var(--status-queued)'
  return 'var(--status-failed, #f87171)'
}

function CookStat({
  dotColor, glow, num, label,
}: {
  dotColor: string
  glow?: boolean
  num: number
  label: string
}) {
  return (
    <span className="inline-flex items-center gap-1.5" style={{ color: 'var(--tp-text)' }}>
      <span
        style={{
          width: 7, height: 7, borderRadius: '50%',
          background: dotColor,
          boxShadow: glow ? `0 0 8px ${dotColor}` : undefined,
          flexShrink: 0,
        }}
      />
      <span className="font-mono font-semibold">{num}</span>
      <span style={{ color: 'var(--tp-text-muted)' }}>{label}</span>
    </span>
  )
}

function TaskList({
  tasks, emptyText, sectionLabel, mode,
}: {
  tasks: ExecutionTask[]
  emptyText: string
  sectionLabel: string
  mode: 'active' | 'history'
}) {
  return (
    <div className="p-3">
      <div
        className="text-[10px] uppercase tracking-wider font-semibold mb-2 px-1"
        style={{ color: 'var(--tp-text-muted)' }}
      >
        {sectionLabel}
      </div>
      {tasks.length === 0 ? (
        <div className="text-xs py-3 text-center" style={{ color: 'var(--tp-text-faint)' }}>
          {emptyText}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {tasks.slice(0, 20).map((t) =>
            mode === 'active'
              ? <ActiveTaskRow key={t.id} task={t} />
              : <HistoryCard key={t.id} task={t} />,
          )}
        </div>
      )}
    </div>
  )
}

