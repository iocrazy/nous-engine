/**
 * QueueProgressOverlay — 真复刻 ComfyUI_frontend/src/components/queue/QueueProgressOverlay.vue。
 *
 * **不是 dock 抽屉,不是 popover** — 是**右上角 350px 浮动 overlay**(对齐 ComfyUI 真实代码)。
 *
 * ## 三态(对齐 ComfyUI `OverlayState`)
 *   - `hidden`:无 active job 且 expanded=false → 不渲染
 *   - `active`:有 running 任务 + expanded=false → 紧凑双进度卡(hover 显底部按钮)
 *   - `expanded`:user 点 chip / "查看全部" → header + tabs + 任务列表(时间分组)
 *
 * 源参考(/tmp/ComfyUI_frontend/src/components/queue/):
 *   QueueProgressOverlay.vue / QueueOverlayActive.vue / QueueOverlayExpanded.vue /
 *   QueueOverlayHeader.vue / job/JobAssetsList.vue / job/JobFilterTabs.vue
 *
 * 用户对照截图三次:#156 popover→drawer / #169 紧凑分组 / 本 PR 真照 ComfyUI 浮动 overlay。
 */
import { useMemo, useRef, useState } from 'react'
import {
  Image as ImageIcon, ListX, MoreHorizontal, Trash2, X,
} from 'lucide-react'

import {
  useCancelTask, useDeleteTask, useRetryTask, useTasks, type ExecutionTask,
} from '../../api/tasks'
import { useExecutionStore } from '../../stores/execution'
import { confirmDialog } from '../../stores/confirm'
import ContextMenu, { type MenuItem } from '../ui/ContextMenu'
import { groupByDate, sortTasks } from '../panels/taskSort'
import { copyTextOrToast } from '../../utils/clipboard'

type JobTab = 'All' | 'Completed' | 'Failed'

const TERMINAL = new Set<ExecutionTask['status']>(['completed', 'failed', 'cancelled'])

function fmtMs(ms: number | null | undefined): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export default function QueueProgressOverlay({
  open, onClose,
}: { open: boolean; onClose: () => void }) {
  const { data: tasks } = useTasks()
  const wfProgress = useExecutionStore((s) => s.progress)
  const nodeProgress = useExecutionStore((s) => s.currentNodeProgress)
  const nodeType = useExecutionStore((s) => s.currentNodeType)
  const previewUrl = useExecutionStore((s) => s.latestPreviewUrl)
  const cancelMutation = useCancelTask()
  const deleteMutation = useDeleteTask()

  const [isHovered, setIsHovered] = useState(false)
  const [tab, setTab] = useState<JobTab>('All')

  const running = useMemo(() => (tasks ?? []).filter((t) => t.status === 'running'), [tasks])
  const queued = useMemo(() => (tasks ?? []).filter((t) => t.status === 'queued'), [tasks])
  const done = useMemo(
    () => sortTasks((tasks ?? []).filter((t) => TERMINAL.has(t.status)), 'created', 'desc'),
    [tasks],
  )
  const hasFailedJobs = useMemo(() => done.some((t) => t.status === 'failed'), [done])

  const isExpanded = open
  const hasActiveJob = running.length > 0 || queued.length > 0
  const isVisible = isExpanded || hasActiveJob
  const showBackground = isExpanded || isHovered

  // 智能 headerTitle(对齐 ComfyUI QueueProgressOverlay headerTitle computed)
  const headerTitle = useMemo(() => {
    if (!hasActiveJob) return '任务队列'
    if (queued.length === 0) return `${running.length} 个正在运行`
    if (running.length === 0) return `${queued.length} 个排队中`
    return `${running.length} 个正在运行,${queued.length} 个排队中`
  }, [hasActiveJob, running.length, queued.length])

  const onInterruptAll = () => {
    for (const r of running) cancelMutation.mutate(r.id)
  }
  const onClearQueuedAll = async () => {
    if (queued.length === 0) return
    if (!(await confirmDialog({ message: `清理 ${queued.length} 个排队任务?`, danger: true, confirmText: '清理' }))) return
    for (const q of queued) {
      cancelMutation.mutate(q.id, { onSettled: () => deleteMutation.mutate(q.id) })
    }
  }
  const onViewAll = () => useExecutionStore.getState().toggleTaskPanel()

  if (!isVisible) return null

  const totalPct = Math.round(wfProgress)
  const nodePct = nodeProgress ?? 0
  const currentNodeName = nodeType || '—'

  return (
    <div
      data-testid="queue-progress-overlay"
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      style={{
        position: 'fixed', top: 60, right: 16,
        width: 350, minWidth: 310, maxHeight: '60vh',
        background: showBackground ? 'var(--bg-elevated)' : 'transparent',
        border: '1px solid ' + (showBackground ? 'var(--border)' : 'transparent'),
        borderRadius: 8,
        boxShadow: showBackground ? 'var(--shadow-lg)' : 'none',
        zIndex: 100,
        display: 'flex', flexDirection: 'column',
        overflow: 'hidden',
        transition: 'background 0.2s, border-color 0.2s, box-shadow 0.2s',
        // FINDING-002(/design-review):ComfyUI 用 font-inter;nous 应明确 fallback
        // 链(避免 var(--font) 落到无 weight 的字体导致字重不一致)。
        fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif',
      }}
    >
      {isExpanded ? (
        <ExpandedView
          headerTitle={headerTitle}
          queuedCount={queued.length}
          done={done}
          hasFailedJobs={hasFailedJobs}
          tab={tab}
          onTabChange={setTab}
          onClose={onClose}
          onClearQueued={onClearQueuedAll}
        />
      ) : (
        <ActiveView
          runningCount={running.length}
          queuedCount={queued.length}
          totalPct={totalPct}
          nodePct={nodePct}
          currentNodeName={currentNodeName}
          previewUrl={previewUrl}
          isHovered={isHovered}
          onViewAll={onViewAll}
          onInterruptAll={onInterruptAll}
          onClearQueued={onClearQueuedAll}
        />
      )}
    </div>
  )
}

// ---------- ActiveView(紧凑 active 卡) ----------

function ActiveView({
  runningCount, queuedCount,
  totalPct, nodePct, currentNodeName, previewUrl,
  isHovered, onViewAll, onInterruptAll, onClearQueued,
}: {
  runningCount: number; queuedCount: number
  totalPct: number; nodePct: number; currentNodeName: string
  previewUrl: string | null
  isHovered: boolean
  onViewAll: () => void
  onInterruptAll: () => void
  onClearQueued: () => void
}) {
  // FINDING-001:active 卡 ComfyUI 不显 headerTitle(只 expanded 用)。
  // FINDING-004:active 内部 gap=3(12px),对齐 ComfyUI gap-3。原 8px 偏紧。
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 8 }}>
      {/* 双 progress bar 叠加(对齐 ComfyUI active overlay) */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div style={{
          position: 'relative', height: 8, width: '100%',
          background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 999,
          overflow: 'hidden',
        }}>
          <div style={{
            position: 'absolute', inset: 0, height: '100%', width: `${totalPct}%`,
            background: 'var(--info)', borderRadius: 999,
            transition: 'width 0.3s ease',
          }} />
          <div style={{
            position: 'absolute', inset: 0, height: '100%', width: `${nodePct}%`,
            background: 'var(--accent)', borderRadius: 999, opacity: 0.85,
            transition: 'width 0.3s ease',
          }} />
        </div>
        <div style={{
          // FINDING-003:ComfyUI text-[12px] leading-none — nous 原 11px 偏小。
          display: 'flex', alignItems: 'flex-start', justifyContent: 'flex-end',
          gap: 16, fontSize: 12, lineHeight: 1,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, color: 'var(--text)', opacity: 0.9 }}>
            <span>全部:</span>
            <span style={{ fontWeight: 700 }}>{totalPct}%</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, color: 'var(--muted)' }}>
            <span style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {currentNodeName}
            </span>
            <span>{nodePct}%</span>
          </div>
        </div>
      </div>

      {/* latent live preview(nous 扩展,ComfyUI 无 — 它的 preview 在 canvas node 上) */}
      {previewUrl && isHovered && (
        <div style={{
          width: '100%', display: 'flex', justifyContent: 'center',
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 4, padding: 4,
        }}>
          <img src={previewUrl} alt="latent preview" style={{
            maxHeight: 96, maxWidth: '100%', borderRadius: 3, imageRendering: 'pixelated',
          }} />
        </div>
      )}

      {/* 底部行(hover 才显,对齐 ComfyUI bottomRowClass opacity 切换) */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 16,
        opacity: isHovered ? 1 : 0,
        pointerEvents: isHovered ? 'auto' : 'none',
        transition: 'opacity 0.2s ease',
        padding: '0 4px',
      }}>
        {/* FINDING-003:ComfyUI bottom row text-[12px] — 字号应 12px */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, color: 'var(--text)' }}>
          <span style={{ fontWeight: 700 }}>{runningCount}</span>
          <span style={{ opacity: 0.9 }}>正在运行</span>
          {runningCount > 0 && (
            <button
              type="button" onClick={onInterruptAll}
              aria-label="中止所有运行"
              style={btnDanger}
            ><X size={12} /></button>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, color: 'var(--text)' }}>
          <span style={{ fontWeight: 700 }}>{queuedCount}</span>
          <span style={{ opacity: 0.9 }}>排队中</span>
          {queuedCount > 0 && (
            <button
              type="button" onClick={onClearQueued}
              aria-label="清理队列"
              style={btnDanger}
            ><ListX size={12} /></button>
          )}
        </div>
        <button
          type="button" onClick={onViewAll}
          style={{
            minWidth: 100, flex: 1, padding: '4px 8px',
            fontSize: 11, fontWeight: 500,
            background: 'var(--bg-hover)', color: 'var(--text)',
            border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer',
          }}
        >
          查看全部
        </button>
      </div>
    </div>
  )
}

// ---------- ExpandedView(列表 + 时间分组) ----------

function ExpandedView({
  headerTitle, queuedCount, done, hasFailedJobs, tab, onTabChange, onClose, onClearQueued,
}: {
  headerTitle: string
  queuedCount: number
  done: ExecutionTask[]
  hasFailedJobs: boolean
  tab: JobTab
  onTabChange: (t: JobTab) => void
  onClose: () => void
  onClearQueued: () => void
}) {
  const deleteMutation = useDeleteTask()
  const [historyMenuPos, setHistoryMenuPos] = useState<{ x: number; y: number } | null>(null)
  const filteredDone = useMemo(() => {
    if (tab === 'All') return done
    if (tab === 'Completed') return done.filter((t) => t.status === 'completed')
    return done.filter((t) => t.status === 'failed')
  }, [tab, done])
  const groups = useMemo(() => groupByDate(filteredDone), [filteredDone])
  // FINDING-005:JobHistoryActionsMenu 等价 — 「清空历史」入口
  const historyMenuItems: MenuItem[] = [
    {
      label: '清空历史记录', danger: true,
      disabled: done.length === 0,
      onClick: async () => {
        if (!(await confirmDialog({ message: `清空 ${done.length} 条历史记录?(不可恢复)`, danger: true, confirmText: '清空' }))) return
        for (const t of done) deleteMutation.mutate(t.id)
      },
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 0, flex: 1, minHeight: 0 }}>
      <div style={{
        height: 48, display: 'flex', alignItems: 'center', gap: 8,
        padding: '0 8px', borderBottom: '1px solid var(--border)', flexShrink: 0,
      }}>
        <div style={{
          flex: 1, minWidth: 0, padding: '0 8px',
          fontSize: 14, fontWeight: 400, color: 'var(--text)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>{headerTitle}</div>
        <div style={{
          // FINDING-003:ComfyUI header tools text-[12px] leading-none
          display: 'inline-flex', alignItems: 'center', gap: 8,
          fontSize: 12, color: 'var(--text)',
        }}>
          <span style={{ opacity: queuedCount === 0 ? 0.5 : 1 }}>清理队列</span>
          <button
            type="button" onClick={onClearQueued} disabled={queuedCount === 0}
            aria-label="清理排队任务"
            title={queuedCount > 0 ? `清理 ${queuedCount} 个排队任务` : '无排队任务'}
            style={{
              width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center',
              background: queuedCount > 0 ? 'var(--error, #ef4444)' : 'transparent',
              color: queuedCount > 0 ? 'white' : 'var(--muted)',
              border: queuedCount > 0 ? 'none' : '1px solid var(--border)',
              borderRadius: 4,
              cursor: queuedCount > 0 ? 'pointer' : 'default',
            }}
          ><ListX size={14} /></button>
        </div>
        {/* FINDING-005:JobHistoryActionsMenu 等价 — ⋯ button 弹「清空历史」menu */}
        <button
          type="button"
          onClick={(e) => setHistoryMenuPos({ x: e.clientX, y: e.clientY })}
          aria-label="更多操作"
          title="更多操作"
          style={iconBtn}
        ><MoreHorizontal size={14} /></button>
        <button
          type="button" onClick={onClose} aria-label="关闭"
          style={iconBtn}
        ><X size={14} /></button>
      </div>

      <div style={{
        display: 'flex', alignItems: 'center', gap: 4, padding: '8px 12px', flexShrink: 0,
      }}>
        <TabBtn active={tab === 'All'} onClick={() => onTabChange('All')}>全部</TabBtn>
        <TabBtn active={tab === 'Completed'} onClick={() => onTabChange('Completed')}>已完成</TabBtn>
        {hasFailedJobs && (
          <TabBtn active={tab === 'Failed'} onClick={() => onTabChange('Failed')}>失败</TabBtn>
        )}
      </div>

      <div style={{ flex: 1, overflowY: 'auto', paddingBottom: 16 }}>
        {groups.length === 0 && (
          <div style={{
            padding: 32, textAlign: 'center', fontSize: 12, color: 'var(--muted)',
          }}>暂无完成任务</div>
        )}
        {groups.map(([label, items]) => (
          <div key={label}>
            <div style={{
              // FINDING-003:ComfyUI text-xs (12px) leading-none for group header
              padding: '0 12px 8px 12px', fontSize: 12, color: 'var(--muted)', lineHeight: 1,
            }}>{label}</div>
            {items.map((t) => <JobRow key={t.id} task={t} />)}
          </div>
        ))}
      </div>
      {historyMenuPos && (
        <ContextMenu
          items={historyMenuItems}
          position={historyMenuPos}
          onClose={() => setHistoryMenuPos(null)}
        />
      )}
    </div>
  )
}

function TabBtn({ active, onClick, children }:
  { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button" onClick={onClick}
      style={{
        padding: '6px 12px', fontSize: 12, fontWeight: 500,
        background: active ? 'var(--bg-hover)' : 'transparent',
        color: active ? 'var(--text)' : 'var(--muted)',
        border: 'none', borderRadius: 6, cursor: 'pointer',
      }}
    >{children}</button>
  )
}

// ---------- JobRow(h-12 紧凑行,hover 显 actions,对齐 ComfyUI AssetsListItem) ----------

function JobRow({ task }: { task: ExecutionTask }) {
  const retryMutation = useRetryTask()
  const cancelMutation = useCancelTask()
  const deleteMutation = useDeleteTask()
  const [hover, setHover] = useState(false)
  const [menuPos, setMenuPos] = useState<{ x: number; y: number } | null>(null)
  const rowRef = useRef<HTMLDivElement>(null)
  const thumb = task.output_thumbnails?.[0]
  const title = task.workflow_name || `任务 ${String(task.id).slice(0, 8)}`
  const meta = fmtMs(task.duration_ms)
  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault(); setMenuPos({ x: e.clientX, y: e.clientY })
  }
  const isFailed = task.status === 'failed'
  const isCompleted = task.status === 'completed'
  const menuItems: MenuItem[] = [
    ...(thumb ? [{ label: '查看图片', onClick: () => window.open(thumb, '_blank') }] : []),
    { label: '复制任务 ID', onClick: () => void copyTextOrToast(String(task.id), '已复制任务 ID') },
    ...(thumb ? [{
      label: '下载',
      onClick: () => {
        const a = document.createElement('a'); a.href = thumb; a.download = `nous-${task.id}.png`; a.click()
      },
    }] : []),
    { label: '', divider: true } as MenuItem,
    {
      label: '重试',
      disabled: task.status !== 'failed' && task.status !== 'cancelled',
      onClick: () => retryMutation.mutate(task.id),
    },
    { label: '删除', danger: true, onClick: () => deleteMutation.mutate(task.id) },
  ]
  return (
    <>
      <div
        ref={rowRef}
        onContextMenu={onContextMenu}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        style={{
          height: 48, display: 'flex', alignItems: 'center', gap: 8,
          padding: '0 12px',
          background: hover ? 'var(--bg-hover)' : 'transparent',
          transition: 'background 0.1s',
          cursor: 'context-menu',
        }}
      >
        <div style={{
          width: 40, height: 40, flexShrink: 0,
          background: 'var(--bg)', borderRadius: 4,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          overflow: 'hidden',
        }}>
          {thumb
            ? <img src={thumb} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
            : <ImageIcon size={18} style={{ color: 'var(--muted)' }} />}
        </div>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{
            fontSize: 13, color: 'var(--text)', fontWeight: 500,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{title}</div>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>{meta}</div>
        </div>
        {hover && (
          <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
            {isFailed && (
              <button
                type="button" aria-label="删除"
                onClick={(e) => { e.stopPropagation(); deleteMutation.mutate(task.id) }}
                style={btnDanger}
              ><Trash2 size={14} /></button>
            )}
            {isCompleted && thumb && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); window.open(thumb, '_blank') }}
                style={{
                  padding: '2px 8px', fontSize: 11, color: 'var(--accent)',
                  background: 'transparent', border: 'none', cursor: 'pointer',
                }}
              >查看</button>
            )}
            {task.status === 'running' && (
              <button
                type="button" aria-label="中止"
                onClick={(e) => { e.stopPropagation(); cancelMutation.mutate(task.id) }}
                style={btnDanger}
              ><X size={14} /></button>
            )}
            <button
              type="button" aria-label="更多"
              onClick={(e) => { e.stopPropagation(); setMenuPos({ x: e.clientX, y: e.clientY }) }}
              style={iconBtn}
            ><MoreHorizontal size={14} /></button>
          </div>
        )}
      </div>
      {menuPos && (
        <ContextMenu items={menuItems} position={menuPos} onClose={() => setMenuPos(null)} />
      )}
    </>
  )
}

const iconBtn: React.CSSProperties = {
  width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'transparent', border: 'none', color: 'var(--muted)',
  borderRadius: 4, cursor: 'pointer',
}

const btnDanger: React.CSSProperties = {
  width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'var(--error, #ef4444)', border: 'none', color: 'white',
  borderRadius: 4, cursor: 'pointer',
}
