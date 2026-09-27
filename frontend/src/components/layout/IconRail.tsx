import {
  LayoutDashboard,
  Layers,
  GitBranch,
  Activity,
  KeyRound,
  BarChart3,
  ScrollText,
  SlidersHorizontal,
  Sun,
  Moon,
  Monitor,
  ListTodo,
  LogOut,
  Images,
  HeartPulse,
} from 'lucide-react'
import { useNavigate, useLocation } from 'react-router-dom'
import { usePanelStore, type PanelId, type OverlayId } from '../../stores/panel'
import { useThemeStore } from '../../stores/theme'
import { useExecutionStore } from '../../stores/execution'
import { useAdminLogout, useAdminMe } from '../../api/admin'

// v3 IA: 8 main navs (Dashboard / Services / Workflow / Engines / API Key /
// Usage / Logs / Settings) + 3 theme buttons + GPU pill.
//
// Workflow is the panel-style entry (opens the canvas tabs); the rest are
// overlay-style routes.

const OVERLAY_ROUTES: Record<OverlayId, string> = {
  dashboard: '/dashboard',
  models: '/models',
  services: '/services',
  apps: '/apps',
  'api-keys-list': '/api-keys',
  'api-key-detail': '/api-keys',
  agents: '/agents',
  settings: '/settings',
  logs: '/logs',
  'node-packages': '/node-packages',
  'preset-detail': '/',
  'service-detail': '/services',
  'workflows-list': '/workflows',
  usage: '/usage',
  history: '/history',
  status: '/status',
}

// Workflow goes through the overlay nav now (v3 m08 list); the in-canvas
// side panel is still available via PanelId='workflows' from inside the
// editor at /workflows/:id, but the rail no longer toggles it.
const PANEL_ITEMS: { id: PanelId; icon: typeof GitBranch; label: string }[] = []

const TOP_NAVS: { id: OverlayId; icon: typeof LayoutDashboard; label: string }[] = [
  { id: 'dashboard', icon: LayoutDashboard, label: 'Dashboard' },
  { id: 'services', icon: Activity, label: '服务' },
  { id: 'workflows-list', icon: GitBranch, label: 'Workflow' },
]

const MID_NAVS: { id: OverlayId; icon: typeof Layers; label: string }[] = [
  { id: 'models', icon: Layers, label: '引擎库' },
  { id: 'api-keys-list', icon: KeyRound, label: 'API Key' },
  { id: 'history', icon: Images, label: '历史' },
  { id: 'usage', icon: BarChart3, label: '用量' },
  { id: 'status', icon: HeartPulse, label: '状态' },
  { id: 'logs', icon: ScrollText, label: '日志' },
]

export default function IconRail() {
  const { activePanel, activeOverlay, togglePanel } = usePanelStore()
  const { mode, setMode } = useThemeStore()
  const navigate = useNavigate()
  const location = useLocation()
  const logout = useAdminLogout()
  const me = useAdminMe()
  // Only render the logout button when admin gate is actually active —
  // dev mode (ADMIN_PASSWORD empty) has no session to log out of.
  const canLogout = me.data?.login_required === true

  const navigateOverlay = (id: OverlayId) => {
    const target = OVERLAY_ROUTES[id]
    if (activeOverlay === id || location.pathname === target) {
      navigate('/')
    } else {
      navigate(target)
    }
  }

  return (
    <div
      className="flex flex-col items-center shrink-0 z-20"
      style={{
        width: 48,
        background: 'var(--bg-accent)',
        borderRight: '1px solid var(--border)',
        padding: '8px 0',
      }}
    >
      {/* Logo */}
      <div
        className="flex items-center justify-center font-extrabold text-white text-[13px] mb-3"
        style={{
          width: 28,
          height: 28,
          borderRadius: 6,
          background: 'var(--accent)',
        }}
      >
        N
      </div>

      {/* Top: Dashboard, Services */}
      {TOP_NAVS.map(({ id, icon: Icon, label }) => (
        <RailButton
          key={id}
          active={
            activeOverlay === id ||
            (id === 'services' && activeOverlay === 'service-detail')
          }
          onClick={() => navigateOverlay(id)}
          label={label}
        >
          <Icon size={18} />
        </RailButton>
      ))}

      <Sep />

      {/* Workflow (panel) */}
      {PANEL_ITEMS.map(({ id, icon: Icon, label }) => (
        <RailButton
          key={id}
          active={activePanel === id && !activeOverlay}
          onClick={() => {
            if (location.pathname !== '/') navigate('/')
            togglePanel(id)
          }}
          label={label}
        >
          <Icon size={18} />
        </RailButton>
      ))}

      <Sep />

      {/* Mid: Engines, API Key, Usage, Logs */}
      {MID_NAVS.map(({ id, icon: Icon, label }) => (
        <RailButton
          key={id}
          active={
            activeOverlay === id ||
            (id === 'api-keys-list' && activeOverlay === 'api-key-detail')
          }
          onClick={() => navigateOverlay(id)}
          label={label}
        >
          <Icon size={18} />
        </RailButton>
      ))}

      {/* Bottom section */}
      <div className="mt-auto flex flex-col items-center gap-1">
        <TaskRailButton />

        <RailButton
          active={activeOverlay === 'settings'}
          onClick={() => navigateOverlay('settings')}
          label="设置"
        >
          <SlidersHorizontal size={18} />
        </RailButton>

        <Sep />

        <ThemeButton active={mode === 'light'} onClick={() => setMode('light')}>
          <Sun size={12} />
        </ThemeButton>
        <ThemeButton active={mode === 'dark'} onClick={() => setMode('dark')}>
          <Moon size={12} />
        </ThemeButton>
        <ThemeButton active={mode === 'auto'} onClick={() => setMode('auto')}>
          <Monitor size={12} />
        </ThemeButton>

        {canLogout && (
          <>
            <Sep />
            <button
              onClick={() => {
                if (logout.isPending) return
                logout.mutate()
              }}
              title="退出登录"
              aria-label="退出登录"
              className="flex items-center justify-center"
              style={{
                width: 32,
                height: 28,
                borderRadius: 4,
                border: '1px solid var(--border)',
                background: 'transparent',
                color: 'var(--muted)',
                cursor: logout.isPending ? 'wait' : 'pointer',
                opacity: logout.isPending ? 0.5 : 1,
              }}
            >
              <LogOut size={12} />
            </button>
          </>
        )}

        <div className="flex items-center gap-1 mt-1" style={{ fontSize: 8, color: 'var(--muted)' }}>
          <span
            className="inline-block rounded-full"
            style={{ width: 5, height: 5, background: 'var(--ok)' }}
          />
          GPU
        </div>
      </div>
    </div>
  )
}

function Sep() {
  return (
    <div
      className="my-1.5"
      style={{ width: 24, height: 1, background: 'var(--border)' }}
    />
  )
}

function RailButton({
  active,
  onClick,
  label,
  children,
}: {
  active: boolean
  onClick: () => void
  label: string
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      className="group relative flex items-center justify-center mb-0.5"
      style={{
        width: 36,
        height: 36,
        borderRadius: 6,
        border: 'none',
        background: active ? 'var(--accent-subtle)' : 'transparent',
        color: active ? 'var(--accent)' : 'var(--muted)',
        cursor: 'pointer',
        transition: 'all 0.12s',
      }}
      onMouseEnter={(e) => {
        if (!active) {
          e.currentTarget.style.background = 'var(--bg-hover)'
          e.currentTarget.style.color = 'var(--text)'
        }
      }}
      onMouseLeave={(e) => {
        if (!active) {
          e.currentTarget.style.background = 'transparent'
          e.currentTarget.style.color = 'var(--muted)'
        }
      }}
    >
      {children}
      <span
        className="hidden group-hover:block absolute pointer-events-none"
        style={{
          left: 44,
          top: '50%',
          transform: 'translateY(-50%)',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          borderRadius: 4,
          padding: '3px 8px',
          fontSize: 10,
          whiteSpace: 'nowrap',
          color: 'var(--text)',
          zIndex: 100,
        }}
      >
        {label}
      </span>
    </button>
  )
}

function TaskRailButton() {
  const { taskPanelOpen, toggleTaskPanel, taskIconBadge, clearTaskBadge } = useExecutionStore()

  const handleClick = () => {
    // 打开面板即视为「已查看」—— 清掉未查看计数（spec §6.3 DD4）。
    if (!taskPanelOpen) clearTaskBadge()
    toggleTaskPanel()
  }

  return (
    <div className="relative">
      <RailButton active={taskPanelOpen} onClick={handleClick} label="Tasks">
        <ListTodo size={18} />
      </RailButton>
      {taskIconBadge > 0 && (
        <span
          aria-label={`${taskIconBadge} 个新任务`}
          className="absolute pointer-events-none"
          style={{
            top: 2,
            right: 2,
            minWidth: 14,
            height: 14,
            borderRadius: 7,
            background: 'var(--accent)',
            color: '#fff',
            fontSize: 9,
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '0 3px',
          }}
        >
          {taskIconBadge}
        </span>
      )}
    </div>
  )
}

function ThemeButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center justify-center"
      style={{
        width: 32,
        height: 28,
        borderRadius: 4,
        border: '1px solid var(--border)',
        background: active ? 'var(--bg-elevated)' : 'transparent',
        color: active ? 'var(--text-strong)' : 'var(--muted)',
        cursor: 'pointer',
        fontSize: 11,
      }}
    >
      {children}
    </button>
  )
}
