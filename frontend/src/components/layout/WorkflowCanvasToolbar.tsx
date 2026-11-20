/**
 * WorkflowCanvasToolbar — workflow editor 画布浮动工具栏(PR-2b,任务面板重置)。
 *
 * 取代原来挂在主区顶部的 Topbar(Workspace + Run + Templates + Clear + 发布服务)。
 * 改为**画布内顶部正中**浮动 chip,只在 workflow editor 路由出现(/workflows/:id 等)。
 *
 * D5 决策:工作流 Topbar 不放主顶,改画布内浮动条;Run/任务/Templates/Clear/发布服务
 * 五个动作 chip 全在一行。任务图标已被 GlobalTopbar 右侧接管,这里保留兼容入口
 * (workflow editor 内不引导用户跳回顶部点击)。
 */
import { useEffect, useCallback, useState } from 'react'
import { Play, Loader, Square } from 'lucide-react'
import PublishDialog from '../workflow/PublishDialog'
import { useLocation } from 'react-router-dom'
import { useWorkspaceStore } from '../../stores/workspace'
import { confirmDialog } from '../../stores/confirm'
import { usePanelStore } from '../../stores/panel'
import { useExecutionStore } from '../../stores/execution'
import { executeWorkflow } from '../../utils/workflowExecutor'
import { nextSeed, type SeedControlMode } from '../../utils/seedControl'
import { useToastStore } from '../../stores/toast'
import { useUnpublishWorkflow } from '../../api/workflows'
import { useTasks, useCancelTask } from '../../api/tasks'
import { useNotificationStore } from '../../stores/notifications'

export default function WorkflowCanvasToolbar() {
  const { tabs, activeTabId } = useWorkspaceStore()
  const getActiveWorkflow = useWorkspaceStore((s) => s.getActiveWorkflow)
  const setWorkflow = useWorkspaceStore((s) => s.setWorkflow)
  const updateNode = useWorkspaceStore((s) => s.updateNode)
  const { activeOverlay } = usePanelStore()
  const { isRunning, progress, start, succeed, fail, resetNodeStates, bumpTaskBadge } =
    useExecutionStore()
  const toast = useToastStore((s) => s.add)
  const unpublishWf = useUnpublishWorkflow()
  const requestNotifyPermission = useNotificationStore((s) => s.requestPermission)

  // 运行中断:image/plugin 工作流 /execute 入队即返回 202,store 的 isRunning 不持续
  // 整个后端跑(succeed() 在 enqueue 后立刻置 false)。真正「有任务在跑」的权威来源
  // 是 task list —— 找 running/queued 的任务,提供 Stop(对齐 ComfyUI Interrupt)。
  // 后端 cancel 端点对 running 任务是**真打断**(cancel_flag + diffusers callback 步边界
  // raise CancelledError),不只是改 DB 状态。
  const { data: tasks } = useTasks()
  const cancelTask = useCancelTask()
  const activeTask = tasks?.find((t) => t.status === 'running' || t.status === 'queued')
  const runDisabled = isRunning || !!activeTask

  const handleStop = () => {
    if (!activeTask) return
    cancelTask.mutate(activeTask.id, {
      onSuccess: () => toast('已请求中止当前任务', 'info'),
      onError: (e) => toast(e instanceof Error ? e.message : '中止失败', 'error'),
    })
  }

  const activeTab = tabs.find((t) => t.id === activeTabId)
  const isPublished = activeTab?.workflow?.status === 'published'
  const [showPublishWizard, setShowPublishWizard] = useState(false)
  const location = useLocation()

  // ?publish=1 query 自动弹发布对话框(保留原 Topbar 行为)。
  useEffect(() => {
    const params = new URLSearchParams(location.search)
    if (params.get('publish') !== '1') return
    if (!activeTab?.workflow) return
    setShowPublishWizard(true)
  }, [activeTab, location.search])

  // ComfyUI control_after_generate:遍历工作流里带 control_after_generate 的节点(KSampler),
  // 按模式把 seed 更新成下一个值。在 Run 完成后调,改的是 store 里的节点 data(下次 Run 用新 seed)。
  const applySeedControl = (workflow: ReturnType<typeof getActiveWorkflow>) => {
    for (const node of workflow.nodes) {
      const mode = node.data?.control_after_generate as SeedControlMode | undefined
      if (!mode || mode === 'fixed') continue
      const cur = node.data?.seed as string | number | undefined
      const updated = nextSeed(cur, mode)
      updateNode(node.id, { seed: updated })
    }
  }

  const handleRun = async () => {
    if (isRunning) return
    void requestNotifyPermission()
    const workflow = getActiveWorkflow()
    start()
    try {
      const result = await executeWorkflow(workflow)
      const taskId = (result as { task_id?: string })?.task_id
      bumpTaskBadge()
      toast(taskId ? `任务已入队 · ${taskId}` : '任务已入队', 'info')
      succeed(null)
      resetNodeStates()
      // ComfyUI control_after_generate:Run 完成后按模式更新各 KSampler 的 seed
      // (fixed 不动 / increment+1 / decrement-1 / randomize 随机)。纯前端,下次 Run 生效。
      applySeedControl(workflow)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Unknown error'
      fail(msg)
      toast(msg, 'error')
      setTimeout(() => resetNodeStates(), 5000)
    }
  }

  const handleRunCb = useCallback(() => { handleRun() }, [isRunning]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault()
        handleRunCb()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [handleRunCb])

  // overlay 路由不显示(只 workflow editor 路由出来)
  if (activeOverlay) return null

  return (
    <div
      className="absolute z-20 flex items-center gap-1.5 px-2 py-1.5 rounded-lg shadow-lg"
      style={{
        top: 12,
        left: '50%',
        transform: 'translateX(-50%)',
        background: 'var(--tp-bg-card)',
        border: '1px solid var(--tp-border-strong)',
        backdropFilter: 'blur(8px)',
      }}
    >
      {/* Workspace 名 + 状态点(精简版,只保留 dirty/saved 指示)*/}
      {activeTab && (
        <div className="flex items-center gap-1.5 px-2" style={{ color: 'var(--tp-text)' }}>
          <span className="text-xs font-medium">{activeTab.name}</span>
          <span
            style={{
              width: 6, height: 6, borderRadius: '50%',
              background: activeTab.isDirty
                ? '#ff9f0a'
                : activeTab.dbId ? '#34c759' : 'transparent',
            }}
            title={activeTab.isDirty ? '未保存' : activeTab.dbId ? '已保存' : ''}
          />
          {activeTab.dbId && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded"
              style={{
                background: isPublished ? 'rgba(52,199,89,0.15)' : 'rgba(255,255,255,0.06)',
                color: isPublished ? '#34c759' : 'var(--tp-text-muted)',
                border: `1px solid ${isPublished ? 'rgba(52,199,89,0.3)' : 'var(--tp-border)'}`,
              }}
            >
              {isPublished ? 'published' : 'draft'}
            </span>
          )}
        </div>
      )}

      <Sep />

      {/* PR-2c D8 决策:删 TaskMenuButton —— 任务管理是全局的,只在 GlobalTopbar 右上
          ListTodo 入口出现,不与 workflow editor 的 Run/Publish 同列。 */}

      <ToolbarBtn onClick={() => {/* TODO templates */}}>Templates</ToolbarBtn>
      <ToolbarBtn
        onClick={async () => {
          if (await confirmDialog({ message: '清空当前工作流的所有节点和连线?', danger: true, confirmText: '清空' })) {
            const wf = getActiveWorkflow()
            setWorkflow({ ...wf, nodes: [], edges: [] })
          }
        }}
      >
        Clear
      </ToolbarBtn>
      <ToolbarBtn primary onClick={handleRun} disabled={runDisabled}>
        <span className="inline-flex items-center gap-1">
          {runDisabled ? (
            <Loader size={12} style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <Play size={12} />
          )}
          {runDisabled ? 'Running…' : 'Run'}
        </span>
      </ToolbarBtn>
      {activeTask && (
        <button
          onClick={handleStop}
          disabled={cancelTask.isPending}
          title="中止当前运行(对齐 ComfyUI Interrupt)"
          className="px-2.5 py-1 rounded text-xs transition-colors"
          style={{
            border: '1px solid var(--error, #ef4444)',
            background: 'transparent',
            color: 'var(--error, #ef4444)',
            cursor: cancelTask.isPending ? 'wait' : 'pointer',
            opacity: cancelTask.isPending ? 0.6 : 1,
          }}
        >
          <span className="inline-flex items-center gap-1">
            <Square size={11} />
            {cancelTask.isPending ? '中止中…' : 'Stop'}
          </span>
        </button>
      )}
      {activeTab?.workflow && (
        isPublished ? (
          <button
            onClick={() => activeTab?.dbId && unpublishWf.mutate(activeTab.dbId)}
            disabled={unpublishWf.isPending || !activeTab?.dbId}
            title="取消发布后服务将下线，新调用会 404"
            className="px-3 py-1 rounded text-xs"
            style={{
              background: 'var(--ok, #34c759)',
              color: '#fff',
              cursor: unpublishWf.isPending ? 'wait' : 'pointer',
              opacity: unpublishWf.isPending ? 0.6 : 1,
              border: 'none',
            }}
          >
            {unpublishWf.isPending ? '取消中…' : '取消发布'}
          </button>
        ) : (
          <button
            onClick={() => setShowPublishWizard(true)}
            className="px-3 py-1 rounded text-xs"
            style={{ background: 'var(--accent)', color: 'var(--on-accent)', border: 'none', cursor: 'pointer' }}
          >
            发布服务
          </button>
        )
      )}
      {activeTab?.workflow && (
        <PublishDialog
          open={showPublishWizard}
          onClose={() => setShowPublishWizard(false)}
          workflowId={String(activeTab.workflow.id)}
          nodes={(activeTab.workflow.nodes ?? []) as Array<{ id: string; type?: string; data?: Record<string, unknown>; position?: { x: number; y: number } }>}
          edges={(activeTab.workflow.edges ?? []) as Array<{ source: string; target: string }>}
        />
      )}

      {/* 运行中底部细进度条 */}
      {isRunning && (
        <div
          style={{
            position: 'absolute', bottom: -1, left: 0, right: 0, height: 2,
            background: 'var(--tp-border-faint)', borderRadius: '0 0 8px 8px', overflow: 'hidden',
          }}
        >
          <div
            style={{
              height: '100%', width: `${progress}%`,
              background: 'var(--status-running)', transition: 'width 0.3s ease',
            }}
          />
        </div>
      )}
    </div>
  )
}

function ToolbarBtn({
  primary, children, onClick, disabled,
}: {
  primary?: boolean
  children: React.ReactNode
  onClick?: () => void
  disabled?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="px-2.5 py-1 rounded text-xs transition-colors"
      style={{
        border: `1px solid ${primary ? 'var(--accent)' : 'var(--tp-border-strong)'}`,
        background: primary ? 'var(--accent)' : 'transparent',
        color: primary ? '#fff' : 'var(--tp-text-muted)',
        cursor: disabled ? 'wait' : 'pointer',
        opacity: disabled ? 0.6 : 1,
      }}
    >
      {children}
    </button>
  )
}

function Sep() {
  return <div style={{ width: 1, height: 16, background: 'var(--tp-border-strong)' }} />
}
