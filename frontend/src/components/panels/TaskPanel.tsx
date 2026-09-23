import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Ban,
  Check,
  CheckCircle2,
  ExternalLink,
  Image as ImageIcon,
  ListFilter,
  Maximize2,
  Minimize2,
  RotateCcw,
  Trash2,
  X,
  XCircle,
  Zap,
} from 'lucide-react'
import {
  useCancelTask,
  useDeleteTask,
  useRetryTask,
  useTasks,
  type ExecutionTask,
} from '../../api/tasks'
import { useExecutionStore } from '../../stores/execution'
import { confirmDialog } from '../../stores/confirm'
import { useTaskProgressStore } from '../../stores/taskProgress'
import {
  ALL_TASK_STATUSES,
  usePanelStore,
  type TaskSortDir,
  type TaskSortKey,
  type TaskStatus,
} from '../../stores/panel'
import ContextMenu, { type MenuItem } from '../ui/ContextMenu'
import { SORT_LABEL, STATUS_LABEL, sortTasks } from './taskSort'
import { copyTextOrToast } from '../../utils/clipboard'

/**
 * TaskPanel — 对齐 ComfyUI 「任务历史」面板风格(读用户截图复刻):
 *
 * 两种模式(panel store taskPanelMode 持久):
 *  - dock(默认):右侧 460px 抽屉,带半透明遮罩,modal-ish。
 *  - float:右下角 360x480 浮窗,无遮罩,不阻塞 canvas 操作(可同时点节点/Run)。
 *
 * Tabs:全部 / 已完成。
 * 运行中任务 = 工作流名 + 耗时 + 进度条 + cancel 按钮。
 * 已完成 = 缩略图(output_thumbnails[0])+ 标识 + 耗时 + 右键菜单(查看图片/复制ID/重试/删除)。
 */

const TERMINAL: ReadonlySet<ExecutionTask['status']> = new Set([
  'completed',
  'failed',
  'cancelled',
])
const RUNNING: ReadonlySet<ExecutionTask['status']> = new Set(['queued', 'running'])

function fmtMs(ms: number | null | undefined): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function tickElapsed(createdAt: string): string {
  // 运行中 tick:从 created_at 到现在 (秒)。
  const start = new Date(createdAt).getTime()
  const sec = Math.max(0, Math.floor((Date.now() - start) / 1000))
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}m ${s.toString().padStart(2, '0')}s`
}

function statusColor(s: ExecutionTask['status']): string {
  if (s === 'completed') return 'var(--ok)'
  if (s === 'failed') return 'var(--error, #ef4444)'
  if (s === 'cancelled') return 'var(--muted)'
  if (s === 'running') return 'var(--info)'
  return 'var(--warn)' // queued
}

function statusLabel(s: ExecutionTask['status']): string {
  return ({ queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消' } as const)[s]
}

export default function TaskPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { data: tasks } = useTasks()
  const mode = usePanelStore((s) => s.taskPanelMode)
  const setMode = usePanelStore((s) => s.setTaskPanelMode)
  const filterStatuses = usePanelStore((s) => s.taskFilterStatuses)
  const setFilterStatuses = usePanelStore((s) => s.setTaskFilterStatuses)
  const sortKey = usePanelStore((s) => s.taskSortKey)
  const sortDir = usePanelStore((s) => s.taskSortDir)
  const setSort = usePanelStore((s) => s.setTaskSort)
  const [tab, setTab] = useState<'all' | 'done'>('all')
  const [now, setNow] = useState(Date.now())

  // 运行中任务存在 → 1s tick 更新 elapsed 显示。
  useEffect(() => {
    const hasRunning = (tasks ?? []).some((t) => t.status === 'running')
    if (!hasRunning) return
    const h = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(h)
  }, [tasks])

  const active = useMemo(() => (tasks ?? []).filter((t) => RUNNING.has(t.status)), [tasks])
  // header 计数对齐 ComfyUI:"N 个正在运行" 只算真正 running,queued 不混在内。
  const running = useMemo(() => (tasks ?? []).filter((t) => t.status === 'running'), [tasks])
  const done = useMemo(() => (tasks ?? []).filter((t) => TERMINAL.has(t.status)), [tasks])

  // 应用 status filter(empty = 不过滤显示全部;否则只保留集合中的状态)+ sort。
  // header chip「N 个正在运行」/ ClearQueueButton 都基于未过滤的原始集合,不受筛选影响 —
  // 不然「筛掉 queued」会让清理按钮以为没有排队任务,反直觉。
  const visible = useMemo(() => {
    const base = tab === 'all' ? [...active, ...done] : done
    const filtered = filterStatuses.size > 0
      ? base.filter((t) => filterStatuses.has(t.status))
      : base
    return sortTasks(filtered, sortKey, sortDir)
  }, [tab, active, done, filterStatuses, sortKey, sortDir])
  const filterActive = filterStatuses.size < ALL_TASK_STATUSES.length

  if (!open) return null

  const isDock = mode === 'dock'
  const containerStyle: React.CSSProperties = isDock
    ? {
        position: 'fixed', right: 0, top: 0, bottom: 0, width: 460,
        background: 'var(--bg-accent)', borderLeft: '1px solid var(--border)',
        boxShadow: '-8px 0 24px rgba(0,0,0,0.3)', zIndex: 50,
        display: 'flex', flexDirection: 'column',
      }
    : {
        position: 'fixed', right: 16, bottom: 16, width: 360, height: 480,
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        boxShadow: 'var(--shadow-lg)', borderRadius: 8, zIndex: 50,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }

  return (
    <>
      <style>{`@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: 0.3 } }`}</style>
      {/* dock 模式才有遮罩;float 不阻塞 canvas */}
      {isDock && (
        <div
          onClick={onClose}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', zIndex: 49 }}
        />
      )}

      <aside style={containerStyle} aria-label="任务面板">
        {/* header */}
        <div style={{
          padding: isDock ? '14px 18px' : '10px 12px',
          borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <h2 style={{
            flex: 1, margin: 0,
            fontSize: isDock ? 14 : 13, fontWeight: 600, color: 'var(--text)',
          }}>
            {running.length > 0 ? `${running.length} 个正在运行` : '任务面板'}
          </h2>
          {/* PR-E2/F2 header 工具按钮组(对齐 ComfyUI 截图):清理队列 / filter / sort / view */}
          <ClearQueueButton tasks={tasks ?? []} />
          <FilterButton
            statuses={filterStatuses}
            onChange={setFilterStatuses}
            active={filterActive}
          />
          <SortButton sortKey={sortKey} sortDir={sortDir} onChange={setSort} />
          <button
            type="button"
            onClick={() => setMode(isDock ? 'float' : 'dock')}
            aria-label={isDock ? '切换浮窗' : '切换停靠'}
            title={isDock ? '切换浮窗' : '切换停靠'}
            style={iconBtnStyle}
          >
            {isDock ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            style={iconBtnStyle}
          >
            <X size={13} />
          </button>
        </div>

        {/* tab strip */}
        <div style={{
          display: 'flex', gap: 4, padding: '6px 12px',
          borderBottom: '1px solid var(--border)',
        }}>
          <TabBtn active={tab === 'all'} onClick={() => setTab('all')}>全部</TabBtn>
          <TabBtn active={tab === 'done'} onClick={() => setTab('done')}>已完成</TabBtn>
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 10, color: 'var(--muted)', alignSelf: 'center' }}>
            {visible.length} 项
          </span>
        </div>

        {/* body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: isDock ? 12 : 8 }}>
          {visible.length === 0 && (
            <div style={{
              padding: 32, textAlign: 'center', fontSize: 12, color: 'var(--muted)',
            }}>
              {tab === 'done' ? '暂无完成任务' : '暂无任务 — 跑一个 workflow 试试'}
            </div>
          )}
          {visible.map((t) => (
            t.status === 'running' || t.status === 'queued'
              ? <RunningTaskCard key={t.id} task={t} now={now} />
              : <CompletedTaskCard key={t.id} task={t} />
          ))}
        </div>
      </aside>
    </>
  )
}

const iconBtnStyle: React.CSSProperties = {
  width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'transparent', border: 'none', color: 'var(--muted)',
  borderRadius: 4, cursor: 'pointer',
}

function FilterButton({
  statuses,
  onChange,
  active,
}: {
  statuses: ReadonlySet<TaskStatus>
  onChange: (next: ReadonlySet<TaskStatus>) => void
  active: boolean
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])
  const toggle = (s: TaskStatus) => {
    const next = new Set(statuses)
    if (next.has(s)) next.delete(s)
    else next.add(s)
    onChange(next)
  }
  const count = statuses.size
  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="筛选状态"
        aria-haspopup="dialog"
        aria-expanded={open}
        title={active ? `筛选(${count}/${ALL_TASK_STATUSES.length})` : '筛选状态'}
        style={{
          ...iconBtnStyle,
          color: active ? 'var(--accent)' : 'var(--muted)',
          background: open ? 'var(--bg-hover)' : 'transparent',
          position: 'relative',
        }}
      >
        <ListFilter size={13} />
        {active && (
          <span
            style={{
              position: 'absolute', top: 2, right: 2,
              width: 6, height: 6, borderRadius: '50%',
              background: 'var(--accent)',
            }}
          />
        )}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="筛选状态"
          style={{
            position: 'absolute', top: 'calc(100% + 4px)', right: 0,
            width: 180, background: 'var(--bg-elevated)',
            border: '1px solid var(--border)', borderRadius: 6,
            boxShadow: 'var(--shadow-lg)', zIndex: 60, padding: 4,
          }}
        >
          <div style={{
            padding: '4px 8px', fontSize: 10, color: 'var(--muted)',
            display: 'flex', alignItems: 'center', gap: 6,
          }}>
            <span style={{ flex: 1 }}>状态筛选</span>
            <button
              type="button"
              onClick={() => onChange(new Set(ALL_TASK_STATUSES))}
              style={{
                background: 'transparent', border: 'none', color: 'var(--accent)',
                fontSize: 10, cursor: 'pointer', padding: 0,
              }}
            >全选</button>
          </div>
          {ALL_TASK_STATUSES.map((s) => {
            const checked = statuses.has(s)
            return (
              <button
                key={s}
                type="button"
                onClick={() => toggle(s)}
                role="menuitemcheckbox"
                aria-checked={checked}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                  padding: '6px 8px', background: 'transparent', border: 'none',
                  borderRadius: 4, cursor: 'pointer', fontSize: 11,
                  color: 'var(--text)', textAlign: 'left',
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--bg-hover)' }}
                onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
              >
                <span style={{
                  width: 14, height: 14, display: 'flex', alignItems: 'center', justifyContent: 'center',
                  border: '1px solid var(--border)', borderRadius: 3,
                  background: checked ? 'var(--accent)' : 'transparent',
                }}>
                  {checked && <Check size={10} color="white" />}
                </span>
                <span>{STATUS_LABEL[s]}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function SortButton({
  sortKey,
  sortDir,
  onChange,
}: {
  sortKey: TaskSortKey
  sortDir: TaskSortDir
  onChange: (key: TaskSortKey, dir: TaskSortDir) => void
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])
  const isDefault = sortKey === 'created' && sortDir === 'desc'
  const opts: { key: TaskSortKey; dir: TaskSortDir }[] = [
    { key: 'created', dir: 'desc' },
    { key: 'created', dir: 'asc' },
    { key: 'duration', dir: 'desc' },
    { key: 'duration', dir: 'asc' },
  ]
  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label="排序"
        aria-haspopup="dialog"
        aria-expanded={open}
        title={SORT_LABEL[`${sortKey}.${sortDir}`]}
        style={{
          ...iconBtnStyle,
          color: isDefault ? 'var(--muted)' : 'var(--accent)',
          background: open ? 'var(--bg-hover)' : 'transparent',
        }}
      >
        {sortKey === 'created' ? (
          sortDir === 'desc' ? <ArrowDown size={13} /> : <ArrowUp size={13} />
        ) : (
          <ArrowUpDown size={13} />
        )}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="排序"
          style={{
            position: 'absolute', top: 'calc(100% + 4px)', right: 0,
            width: 200, background: 'var(--bg-elevated)',
            border: '1px solid var(--border)', borderRadius: 6,
            boxShadow: 'var(--shadow-lg)', zIndex: 60, padding: 4,
          }}
        >
          <div style={{ padding: '4px 8px', fontSize: 10, color: 'var(--muted)' }}>排序</div>
          {opts.map(({ key, dir }) => {
            const selected = key === sortKey && dir === sortDir
            return (
              <button
                key={`${key}.${dir}`}
                type="button"
                onClick={() => { onChange(key, dir); setOpen(false) }}
                role="menuitemradio"
                aria-checked={selected}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, width: '100%',
                  padding: '6px 8px', background: selected ? 'var(--accent-subtle)' : 'transparent',
                  border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 11,
                  color: selected ? 'var(--accent)' : 'var(--text)', textAlign: 'left',
                }}
                onMouseEnter={(e) => {
                  if (!selected) e.currentTarget.style.background = 'var(--bg-hover)'
                }}
                onMouseLeave={(e) => {
                  if (!selected) e.currentTarget.style.background = 'transparent'
                }}
              >
                <span style={{ width: 14, display: 'flex', justifyContent: 'center' }}>
                  {selected && <Check size={10} />}
                </span>
                <span>{SORT_LABEL[`${key}.${dir}`]}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function TabBtn({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: '4px 10px', fontSize: 11, fontWeight: 500,
        background: active ? 'var(--accent-subtle)' : 'transparent',
        color: active ? 'var(--accent)' : 'var(--muted)',
        border: 'none', borderRadius: 4, cursor: 'pointer',
      }}
    >{children}</button>
  )
}

function ClearQueueButton({ tasks }: { tasks: ExecutionTask[] }) {
  // 清理队列:批量删 cancel queued 任务(running 不动)。
  const cancel = useCancelTask()
  const del = useDeleteTask()
  const queued = tasks.filter((t) => t.status === 'queued')
  const has = queued.length > 0
  const onClick = async () => {
    if (!has) return
    if (!(await confirmDialog({ message: `清理 ${queued.length} 个排队任务?`, danger: true, confirmText: '清理' }))) return
    for (const t of queued) {
      cancel.mutate(t.id, {
        onSettled: () => del.mutate(t.id),
      })
    }
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!has}
      aria-label="清理队列"
      title={has ? `清理 ${queued.length} 个排队任务` : '无排队任务'}
      style={{
        ...iconBtnStyle,
        color: has ? 'var(--text)' : 'var(--muted)',
        cursor: has ? 'pointer' : 'default',
      }}
    >
      <Trash2 size={13} />
    </button>
  )
}

function RunningTaskCard({ task, now: _now }: { task: ExecutionTask; now: number }) {
  const cancel = useCancelTask()
  // round3 #4:节点进度按 **task.id** 从 useTaskProgressStore 取(/ws/tasks `event:
  // "progress"` 按 task 广播),不再读全局 useExecutionStore.currentNode* —— 后者只跟
  // 用户最近一次 Run 挂钩,多任务并发时所有卡会显示同一进度(串台)。对齐 ActiveTaskRow。
  const prog = useTaskProgressStore((s) => s.byTaskId.get(task.id))
  const nodeProgress = prog?.progress ?? null
  const nodeStep = prog?.step != null && prog.totalSteps != null
    ? { done: prog.step, total: prog.totalSteps } : null
  const nodeType = prog?.stage ?? null
  // live preview 后端仍只全局广播(没按 task)→ 只在「这张卡就是 execution store 正在
  // 跟踪的 task」时显示,避免把别的任务的预览图贴到所有卡上。后端按 task 广播 preview 后
  // 可改为 per-task(见 round3 备注)。
  const executionTaskId = useExecutionStore((s) => s.taskId)
  const globalPreviewUrl = useExecutionStore((s) => s.latestPreviewUrl)
  const previewUrl = executionTaskId === task.id ? globalPreviewUrl : null

  const isCancelled = task.status === 'cancelled'
  const elapsed = tickElapsed(task.created_at)
  const wfPct = Math.max(0, Math.min(100, task.nodes_total
    ? Math.round((task.nodes_done / task.nodes_total) * 100)
    : 0))
  const nodePct = nodeProgress ?? 0

  return (
    <div style={{
      padding: 10, marginBottom: 6,
      background: 'var(--card)', border: '1px solid var(--info)',
      borderRadius: 6, display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      {/* 标题行:闪电 + 工作流名 + 耗时 + 中止(对齐 ComfyUI active card)。 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Zap size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
        <span style={{
          flex: 1, fontSize: 12, fontWeight: 500, color: 'var(--text)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {task.workflow_name || `任务 ${String(task.id).slice(0, 8)}`}
        </span>
        <span style={{ fontSize: 10, color: 'var(--muted)' }}>{elapsed}</span>
        {!isCancelled && (
          <button
            type="button"
            onClick={() => cancel.mutate(task.id)}
            disabled={cancel.isPending}
            title="中止"
            aria-label="中止任务"
            style={{ ...iconBtnStyle, color: 'var(--error, #ef4444)' }}
          >
            <XCircle size={14} />
          </button>
        )}
      </div>
      {/* live preview thumbnail(latent → RGB,出图过程图慢慢长出来;PR-F)。 */}
      {previewUrl && (
        <div style={{
          width: '100%', display: 'flex', justifyContent: 'center',
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 4, padding: 4,
        }}>
          <img
            src={previewUrl}
            alt="latent preview"
            style={{
              maxHeight: 160, maxWidth: '100%',
              borderRadius: 3, imageRendering: 'pixelated',
            }}
          />
        </div>
      )}
      {/* 双进度条(ComfyUI 风):全部 + 当前节点 */}
      <ThickProgressBar label={`全部:${wfPct}%`} pct={wfPct} />
      {nodeProgress !== null && (
        <ThinProgressBar
          label={`${nodeType || '当前节点'}${nodeStep ? ` ${nodeStep.done}/${nodeStep.total}` : ''} · ${nodePct}%`}
          pct={nodePct}
        />
      )}
    </div>
  )
}

function ThickProgressBar({ label, pct }: { label: string; pct: number }) {
  return (
    <div
      style={{
        position: 'relative', height: 18, background: 'var(--bg)',
        border: '1px solid var(--border)', borderRadius: 4, overflow: 'hidden',
      }}
    >
      <div
        style={{
          width: `${pct}%`, height: '100%', background: 'var(--info)',
          transition: 'width 0.3s ease',
        }}
      />
      <span
        style={{
          position: 'absolute', inset: 0,
          display: 'flex', alignItems: 'center', paddingLeft: 8,
          fontSize: 10, fontWeight: 600, color: 'var(--text-strong, white)',
          mixBlendMode: 'difference',
        }}
      >
        {label}
      </span>
    </div>
  )
}

function ThinProgressBar({ label, pct }: { label: string; pct: number }) {
  return (
    <div>
      <div style={{ fontSize: 9, color: 'var(--muted)', marginBottom: 2 }}>{label}</div>
      <div
        style={{
          height: 4, background: 'var(--border)', borderRadius: 2, overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${pct}%`, height: '100%', background: 'var(--accent-2, #14b8a6)',
            transition: 'width 0.3s linear',
          }}
        />
      </div>
    </div>
  )
}

function CompletedTaskCard({ task }: { task: ExecutionTask }) {
  const retry = useRetryTask()
  const del = useDeleteTask()
  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null)
  const thumb = task.output_thumbnails?.[0]
  const cardRef = useRef<HTMLDivElement>(null)

  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault()
    setMenuPos({ x: e.clientX, y: e.clientY })
  }

  const menu: MenuItem[] = [
    ...(thumb ? [{
      label: '查看图片',
      onClick: () => window.open(thumb, '_blank'),
    }] : []),
    {
      label: '复制任务 ID',
      onClick: () => void copyTextOrToast(String(task.id), '已复制任务 ID'),
    },
    ...(thumb ? [{
      label: '下载',
      onClick: () => {
        const a = document.createElement('a')
        a.href = thumb
        a.download = `nous-${task.id}.png`
        a.click()
      },
    }] : []),
    { label: '', divider: true } as MenuItem,
    {
      label: '重试',
      onClick: () => retry.mutate(task.id),
      disabled: task.status !== 'failed' && task.status !== 'cancelled',
    },
    {
      label: '删除',
      danger: true,
      onClick: () => del.mutate(task.id),
    },
  ]

  return (
    <>
      <div
        ref={cardRef}
        onContextMenu={onContextMenu}
        style={{
          padding: 8, marginBottom: 4,
          background: 'var(--card)', border: '1px solid var(--border)',
          borderRadius: 6, display: 'flex', alignItems: 'center', gap: 8,
          cursor: 'context-menu',
        }}
      >
        {/* 缩略图 / 占位图标 */}
        <div style={{
          width: 40, height: 40, flexShrink: 0,
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 4, display: 'flex', alignItems: 'center', justifyContent: 'center',
          overflow: 'hidden',
        }}>
          {thumb ? (
            <img src={thumb} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          ) : task.task_type === 'image' ? (
            <ImageIcon size={16} style={{ color: 'var(--muted)' }} />
          ) : task.status === 'failed' ? (
            <XCircle size={16} style={{ color: 'var(--error, #ef4444)' }} />
          ) : task.status === 'cancelled' ? (
            <Ban size={16} style={{ color: 'var(--muted)' }} />
          ) : (
            <CheckCircle2 size={16} style={{ color: 'var(--ok)' }} />
          )}
        </div>
        {/* meta */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{
            fontSize: 12, color: 'var(--text)', fontWeight: 500,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            {task.workflow_name || `任务 ${String(task.id).slice(0, 8)}`}
          </div>
          <div style={{ fontSize: 10, color: 'var(--muted)', display: 'flex', gap: 6 }}>
            <span style={{ color: statusColor(task.status) }}>● {statusLabel(task.status)}</span>
            <span>{fmtMs(task.duration_ms)}</span>
          </div>
        </div>
        {/* 快捷动作 */}
        <div style={{ display: 'flex', gap: 2 }}>
          {(task.status === 'failed' || task.status === 'cancelled') && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); retry.mutate(task.id) }}
              disabled={retry.isPending}
              title="重试"
              aria-label="重试任务"
              style={iconBtnStyle}
            >
              <RotateCcw size={12} />
            </button>
          )}
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setMenuPos({ x: e.clientX, y: e.clientY }) }}
            title="更多"
            aria-label="更多操作"
            style={iconBtnStyle}
          >
            <ExternalLink size={12} />
          </button>
        </div>
      </div>
      {menuPos && (
        <ContextMenu items={menu} position={menuPos} onClose={() => setMenuPos(null)} />
      )}
    </>
  )
}
