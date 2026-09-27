import { useEffect, useLayoutEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, useLocation, useParams } from 'react-router-dom'
import GlobalTopbar from './components/layout/GlobalTopbar'
import StartupBanner from './components/layout/StartupBanner'
import IconRail from './components/layout/IconRail'
import WorkflowCanvasToolbar from './components/layout/WorkflowCanvasToolbar'
import TaskDetailModal from './components/layout/TaskDetailModal'
import NodeEditor from './components/nodes/NodeEditor'
import ToastContainer from './components/common/ToastContainer'
import ConfirmHost from './components/common/ConfirmHost'
import { usePanelStore, overlayForPath } from './stores/panel'
import { useWorkspaceStore } from './stores/workspace'
import { apiFetch } from './api/client'
import type { WorkflowFull } from './api/workflows'
import { useToastStore } from './stores/toast'
import { useAdminMe } from './api/admin'
import Login from './pages/Login'
import { loadPluginDefinitions } from './models/nodeRegistry'
import { useTaskCompletionNotifier } from './hooks/useTaskCompletionNotifier'

/** Syncs the current URL to the panel store's activeOverlay */
function RouteSync() {
  const location = useLocation()
  const setOverlay = usePanelStore((s) => s.setOverlay)

  // useLayoutEffect(非 useEffect):路由→overlay 的映射必须在浏览器**绘制前**完成,
  // 否则首帧 activeOverlay 还是上个值(默认 null)→ 露出底下工作流画布一闪,过后才显示
  // overlay(api-keys/services 等)。layout effect 同步在 paint 前 setState+重渲染,消除闪烁。
  // 首次挂载时 store 初值已由 overlayForPath(window.location) 种好,这里只负责后续导航。
  useLayoutEffect(() => {
    const overlay = overlayForPath(location.pathname)
    if (usePanelStore.getState().activeOverlay !== overlay) setOverlay(overlay)
  }, [location.pathname, setOverlay])

  return null
}

/** When URL is /workflows/:id, activate (or fetch + load) that workflow. */
function WorkflowRouteLoader() {
  const { id } = useParams<{ id: string }>()
  const activateByDbId = useWorkspaceStore((s) => s.activateByDbId)
  const loadFromDb = useWorkspaceStore((s) => s.loadFromDb)

  useEffect(() => {
    if (!id) return
    if (activateByDbId(id)) return
    // Not yet in tabs — fetch from backend and open as a new tab.
    apiFetch<WorkflowFull>(`/api/v1/workflows/${encodeURIComponent(id)}`)
      .then(loadFromDb)
      .catch((err) => {
        useToastStore.getState().add(`加载工作流失败: ${err.message ?? err}`, 'error')
      })
  }, [id, activateByDbId, loadFromDb])

  return null
}

function MainLayout({ workflowRoute }: { workflowRoute?: boolean }) {
  const activeOverlay = usePanelStore((s) => s.activeOverlay)
  const isWorkflowView = !activeOverlay
  // 全局监听任务终态翻转，发完成/失败通知（spec §6.3 DD6）。
  useTaskCompletionNotifier()

  // PR-2b(任务面板重置 D4-D5 修正):工作流的两条二级 nav 都从主顶部移除 ——
  // WorkflowTabs(多 workflow 浏览器式 tabs)暂时不渲染(用户走 GlobalTopbar
  // Workflow tab → 列表切换);工作流 Topbar(Run/Templates/Clear/发布)挪进
  // 画布内浮动条 WorkflowCanvasToolbar(画布顶部正中)。NodeEditor 现在直
  // 占满 isWorkflowView 主区,toolbar 浮在画布上层。
  return (
    <div className="flex flex-col h-screen overflow-hidden" style={{ background: 'var(--bg)' }}>
      <RouteSync />
      {workflowRoute && <WorkflowRouteLoader />}
      <GlobalTopbar />
      <StartupBanner />
      <div className="flex-1 flex overflow-hidden">
        <IconRail />
        <div className="flex-1 flex flex-col overflow-hidden relative">
          {isWorkflowView && <WorkflowCanvasToolbar />}
          <NodeEditor />
        </div>
      </div>
      <ToastContainer />
      <ConfirmHost />
      <TaskDetailModal />
    </div>
  )
}


function AuthGate({ children }: { children: React.ReactNode }) {
  const { data, isLoading } = useAdminMe()
  const [pluginsReady, setPluginsReady] = useState(false)

  const authenticated = data ? !data.login_required || data.authenticated : false

  useEffect(() => {
    if (!authenticated || pluginsReady) return
    loadPluginDefinitions().finally(() => setPluginsReady(true))
  }, [authenticated, pluginsReady])

  if (isLoading) {
    return (
      <div
        className="min-h-screen flex items-center justify-center text-sm"
        style={{ background: 'var(--bg)', color: 'var(--muted)' }}
      >
        加载中…
      </div>
    )
  }
  if (!authenticated) return <Login />
  if (!pluginsReady) {
    return (
      <div
        className="min-h-screen flex items-center justify-center text-sm"
        style={{ background: 'var(--bg)', color: 'var(--muted)' }}
      >
        正在加载节点定义…
      </div>
    )
  }
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthGate>
        <Routes>
          <Route path="/" element={<MainLayout />} />
          <Route path="/workflows" element={<MainLayout />} />
          <Route path="/workflows/:id" element={<MainLayout workflowRoute />} />
          {/* PR-2c:删原 /image /tts /llm placeholder 路由 — 所有功能通过 workflow
              节点搭建(D7 决策),独立 service 路由对用户心智模型不合理。 */}
          <Route path="/models" element={<MainLayout />} />
          <Route path="/services" element={<MainLayout />} />
          <Route path="/apps" element={<MainLayout />} />
          <Route path="/agents" element={<MainLayout />} />
          <Route path="/settings" element={<MainLayout />} />
          <Route path="/dashboard" element={<MainLayout />} />
          <Route path="/api-keys" element={<MainLayout />} />
          <Route path="/api-keys/:id" element={<MainLayout />} />
          <Route path="/logs" element={<MainLayout />} />
          <Route path="/node-packages" element={<MainLayout />} />
          <Route path="/usage" element={<MainLayout />} />
          <Route path="/history" element={<MainLayout />} />
          <Route path="/status" element={<MainLayout />} />
          <Route path="/services/:id" element={<MainLayout />} />
        </Routes>
      </AuthGate>
    </BrowserRouter>
  )
}
