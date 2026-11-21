import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'
import GlobalTopbar from './components/layout/GlobalTopbar'
import StartupBanner from './components/layout/StartupBanner'
import IconRail from './components/layout/IconRail'
import TaskDetailModal from './components/layout/TaskDetailModal'
import Lightbox from './components/nodes/Lightbox'
import ToastContainer from './components/common/ToastContainer'
import ConfirmHost from './components/common/ConfirmHost'
import OverlayLoading from './components/common/OverlayLoading'
import { useAdminMe } from './api/admin'
import Login from './pages/Login'
import { useTaskCompletionNotifier } from './hooks/useTaskCompletionNotifier'

const Dashboard = lazy(() => import('./components/overlays/DashboardOverlay'))
const Models = lazy(() => import('./components/overlays/ModelsOverlay'))
const Settings = lazy(() => import('./components/overlays/SettingsOverlay'))
const Agents = lazy(() => import('./components/overlays/AgentManagementOverlay'))
const Logs = lazy(() => import('./components/overlays/LogsOverlay'))
const Status = lazy(() => import('./components/overlays/StatusOverlay'))
const NodePackages = lazy(() => import('./components/overlays/NodePackagesOverlay'))
const ServicesList = lazy(() => import('./pages/ServicesList'))
const ServiceDetail = lazy(() => import('./pages/ServiceDetailRoute'))
const Usage = lazy(() => import('./pages/UsagePage'))
const ApiKeysList = lazy(() => import('./pages/ApiKeysList'))
const ApiKeyDetail = lazy(() => import('./pages/ApiKeyDetail'))
const Gallery = lazy(() => import('./components/overlays/HistoryOverlay'))

function MainLayout() {
  useTaskCompletionNotifier()

  return (
    <div className="console-shell flex h-screen flex-col overflow-hidden" style={{ background: 'var(--bg)' }}>
      <GlobalTopbar />
      <StartupBanner />
      <div className="flex flex-1 overflow-hidden">
        <IconRail />
        <div className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
          <Suspense fallback={<OverlayLoading />}>
            <Outlet />
          </Suspense>
        </div>
      </div>
      <Lightbox />
      <ToastContainer />
      <ConfirmHost />
      <TaskDetailModal />
    </div>
  )
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const { data, isLoading } = useAdminMe()

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
  if (!data || (data.login_required && !data.authenticated)) return <Login />
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthGate>
        <Routes>
          <Route element={<MainLayout />}>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/models" element={<Models />} />
            <Route path="/services" element={<ServicesList />} />
            <Route path="/services/:id" element={<ServiceDetail />} />
            <Route path="/apps" element={<ServicesList />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/api-keys" element={<ApiKeysList />} />
            <Route path="/api-keys/:id" element={<ApiKeyDetail />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/node-packages" element={<NodePackages />} />
            <Route path="/usage" element={<Usage />} />
            <Route path="/gallery" element={<Gallery />} />
            <Route path="/status" element={<Status />} />
            <Route path="/history" element={<Navigate to="/gallery" replace />} />
            <Route path="/workflows/*" element={<Navigate to="/services" replace />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Route>
        </Routes>
      </AuthGate>
    </BrowserRouter>
  )
}
