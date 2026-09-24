import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  AppWindow,
  ChevronDown,
  Cpu,
  Image as ImageIcon,
  Images,
  Link2,
  MoreHorizontal,
  Plus,
  Power,
  Search,
  Trash2,
} from 'lucide-react'
import {
  endpointFor,
  fetchAutostartPreview,
  preloadModelDetail,
  useDeleteService,
  useServices,
  useSetServiceAutostart,
  type AutostartPreview,
  type ServiceCategory,
  type ServiceRow,
} from '../api/services'
import CreateServiceDialog from '../components/services/CreateServiceDialog'
import ImportComfyDialog from '../components/services/ImportComfyDialog'
import { apiFetch } from '../api/client'
import { useToastStore } from '../stores/toast'
import { confirmDialog } from '../stores/confirm'
import { copyTextOrToast } from '../utils/clipboard'
import ServiceCapabilityChips from '../components/services/ServiceCapabilityChips'
import ModelStatusBadge from '../components/services/ModelStatusBadge'

type FilterTab = 'all' | ServiceCategory | 'comfy_bridge'

const TAB_DEFS: { id: FilterTab; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'llm', label: 'LLM' },
  { id: 'tts', label: 'TTS' },
  { id: 'asr', label: 'ASR' },
  { id: 'vl', label: 'VL' },
  // 图像服务的 category 后端存为 'image'(image_output sink 检测)。少了这个 tab,
  // image 服务会落进 buildCounts 的 image 桶却无任何子 tab 可筛 → 只在「全部」出现、
  // 子 tab 计数和与全部对不上(真机:全部 4 但 LLM1+TTS0+VL0+其他2=3)。
  { id: 'image', label: '图像' },
  { id: 'app', label: '其他' },
  // ComfyUI 桥导入的服务(source_type='comfy_template')横跨各 category,单独给一个
  // 筛选 tab,不进 buildCounts 的 category 桶(避免和上面几个重复计数)。
  { id: 'comfy_bridge', label: 'ComfyUI 桥' },
]

export interface ServicesListProps {
  /** Optional callback when user clicks a row. Defaults to navigating to the
   *  detail page; consumers can override (e.g. open in modal). */
  onOpen?: (id: string) => void
}

export default function ServicesList({ onOpen }: ServicesListProps) {
  const { data: services, isLoading, error } = useServices()
  const navigate = useNavigate()
  const [tab, setTab] = useState<FilterTab>('all')
  const [search, setSearch] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const del = useDeleteService()
  const setAutostart = useSetServiceAutostart()
  const toast = useToastStore((s) => s.add)

  const counts = useMemo(() => buildCounts(services ?? []), [services])
  const comfyBridgeCount = useMemo(
    () => (services ?? []).filter((s) => s.source_type === 'comfy_template').length,
    [services],
  )
  const filtered = useMemo(
    () => filter(services ?? [], tab, search),
    [services, tab, search],
  )

  const goDetail = (id: string) => {
    if (onOpen) onOpen(id)
    else navigate(`/services/${id}`)
  }

  /** 开机启动开关。开之前**先问后端开机会加载哪些模型**并让用户确认 —— 这正是
   *  「没开常驻的模型不该开机占显存」那条规矩的显式例外,必须让人看清代价。 */
  const handleToggleAutostart = async (svc: ServiceRow) => {
    if (svc.autostart) {
      const ok = await confirmDialog({
        title: '取消开机启动',
        message: `取消 "${svc.name}" 的开机启动?\n开机将不再预加载它的模型(首次调用会慢一些)。`,
        confirmText: '取消开机启动',
      })
      if (!ok) return
      setAutostart.mutate(
        { serviceId: svc.id, enabled: false },
        {
          onSuccess: () => toast(`已取消 ${svc.name} 的开机启动`, 'success'),
          onError: (e) => toast(`设置失败：${(e as Error).message}`, 'error'),
        },
      )
      return
    }

    let preview: AutostartPreview
    try {
      preview = await fetchAutostartPreview(svc.id)
    } catch (e) {
      toast(`读取开机加载清单失败：${(e as Error).message}`, 'error')
      return
    }
    const lines = preview.preload_models.map((m) => {
      const detail = preloadModelDetail(m)
      return detail ? `• ${m.name}（${detail}）` : `• ${m.name}`
    })
    const message = lines.length
      ? `开机将自动加载以下模型:\n${lines.join('\n')}`
      : `"${svc.name}" 没有可预加载的模型（图像类服务的组件按需加载）。\n仍要设为开机启动?`
    const ok = await confirmDialog({ title: '设为开机启动', message, confirmText: '开机启动' })
    if (!ok) return
    setAutostart.mutate(
      { serviceId: svc.id, enabled: true },
      {
        onSuccess: () => toast(`已设为开机启动：${svc.name}`, 'success'),
        onError: (e) => toast(`设置失败：${(e as Error).message}`, 'error'),
      },
    )
  }

  const handleDelete = async (svc: ServiceRow) => {
    if (!(await confirmDialog({ message: `确认下线服务 "${svc.name}"?\n此操作不可撤销。`, danger: true, confirmText: '下线' }))) return
    del.mutate(svc.id, {
      onSuccess: () => toast(`已下线 ${svc.name}`, 'success'),
      onError: (e) => toast(`下线失败：${(e as Error).message}`, 'error'),
    })
  }

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        overflow: 'auto',
        background: 'var(--bg)',
      }}
    >
      <div style={{ maxWidth: 1200, margin: '0 auto', padding: 20 }}>
        {/* header */}
        <div style={{ display: 'flex', alignItems: 'flex-start', marginBottom: 14 }}>
          <div style={{ flex: 1 }}>
            <h1 style={{ fontSize: 20, color: 'var(--text)', fontWeight: 600 }}>服务</h1>
            <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
              所有对外可调用的服务实例 · 每条服务 = endpoint + schema + 授权 · 通过 API Key 调用
            </p>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <div style={{ position: 'relative' }}>
              <Search
                size={14}
                style={{
                  position: 'absolute',
                  left: 8,
                  top: '50%',
                  transform: 'translateY(-50%)',
                  color: 'var(--muted)',
                }}
              />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索服务..."
                style={{
                  width: 220,
                  background: 'var(--bg-accent)',
                  color: 'var(--text)',
                  border: '1px solid var(--border)',
                  borderRadius: 4,
                  padding: '7px 9px 7px 28px',
                  fontSize: 12,
                }}
              />
            </div>
            <NewServiceMenu
              onQuickProvision={() => setCreateOpen(true)}
              onPublishFromWorkflow={() => navigate('/workflows')}
              onImportComfy={() => setImportOpen(true)}
            />
          </div>
        </div>

        {/* tabs */}
        <div style={{ display: 'flex', gap: 4, marginBottom: 14 }}>
          {TAB_DEFS.map((t) => {
            const n =
              t.id === 'all'
                ? services?.length ?? 0
                : t.id === 'comfy_bridge'
                  ? comfyBridgeCount
                  : counts[t.id] ?? 0
            const active = tab === t.id
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                style={{
                  padding: '6px 12px',
                  background: active ? 'var(--accent-subtle, rgba(99,102,241,0.1))' : 'transparent',
                  color: active ? 'var(--accent)' : 'var(--muted)',
                  border: '1px solid',
                  borderColor: active ? 'var(--accent)' : 'var(--border)',
                  borderRadius: 4,
                  fontSize: 12,
                  cursor: 'pointer',
                }}
              >
                {t.label} {n}
              </button>
            )
          })}
        </div>

        {/* body */}
        {isLoading && <Loading />}
        {error && <ErrorBlock message={(error as Error).message} />}
        {services && services.length === 0 && <Empty onCreate={() => setCreateOpen(true)} />}
        {filtered && filtered.length > 0 && (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))',
              gap: 12,
            }}
          >
            {filtered.map((svc) => (
              <ServiceCard
                key={svc.id}
                svc={svc}
                onOpen={() => goDetail(svc.id)}
                onPlayground={() => goDetail(svc.id)}
                onOpenWorkflow={(wfId) => navigate(`/workflows/${wfId}`)}
                onDelete={() => handleDelete(svc)}
                onToggleAutostart={() => handleToggleAutostart(svc)}
              />
            ))}
          </div>
        )}

        <FooterHint />

        <CreateServiceDialog
          open={createOpen}
          onClose={() => setCreateOpen(false)}
          onCreated={(id) => goDetail(id)}
        />
        <ImportComfyDialog
          open={importOpen}
          onClose={() => setImportOpen(false)}
          onImported={async (serviceName) => {
            setImportOpen(false)
            // POST /comfy-templates 只回模板自己的 id,不带新建 ServiceInstance 的
            // id(services.name===service_name 是由后端唯一性校验保证的不变量,见
            // comfy_templates.py create_template)。GET /services 列表按 name 找一次
            // 拿 svc.id 是目前唯一能跳详情页的办法;拿到后 goDetail 触发的路由切换会
            // 让服务详情页自己的 useService 去 fetch,不需要在这里手动碰 query cache。
            try {
              const rows = await apiFetch<ServiceRow[]>('/api/v1/services')
              const row = rows.find((r) => r.name === serviceName)
              if (row) {
                goDetail(row.id)
                return
              }
            } catch {
              /* fall through to toast below */
            }
            toast(`已导入 ${serviceName},请在列表中打开查看`, 'success')
          }}
        />
      </div>
    </div>
  )
}

// ---------- new service split-button ----------

function NewServiceMenu({
  onQuickProvision,
  onPublishFromWorkflow,
  onImportComfy,
}: {
  onQuickProvision: () => void
  onPublishFromWorkflow: () => void
  onImportComfy: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          padding: '7px 12px',
          background: 'var(--accent)',
          color: '#fff',
          border: 'none',
          borderRadius: 4,
          fontSize: 12,
          cursor: 'pointer',
        }}
      >
        <Plus size={14} />
        新建服务
        <ChevronDown size={12} style={{ marginLeft: 2 }} />
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            right: 0,
            minWidth: 280,
            background: 'var(--bg-elevated, var(--bg))',
            border: '1px solid var(--border)',
            borderRadius: 6,
            boxShadow: '0 12px 32px rgba(0,0,0,0.5)',
            padding: '4px 0',
            zIndex: 30,
          }}
        >
          <MenuItem
            title="快速开通"
            desc="选引擎 · 填参数 · 直接得到服务（适合 LLM/TTS/VL 单步）"
            onClick={() => {
              setOpen(false)
              onQuickProvision()
            }}
          />
          <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />
          <MenuItem
            title="从 Workflow 发布"
            desc="挑一个 Workflow · 指定输入/输出 · 发布（适合多步流程）"
            onClick={() => {
              setOpen(false)
              onPublishFromWorkflow()
            }}
          />
          <div style={{ height: 1, background: 'var(--border)', margin: '4px 0' }} />
          <MenuItem
            title="导入 ComfyUI 工作流"
            desc="上传 Export (API) 导出的 workflow.json · 生成一条「桥」服务"
            onClick={() => {
              setOpen(false)
              onImportComfy()
            }}
          />
        </div>
      )}
    </div>
  )
}

function MenuItem({
  title,
  desc,
  onClick,
}: {
  title: string
  desc: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'flex-start',
        gap: 2,
        padding: '10px 14px',
        background: 'transparent',
        border: 'none',
        cursor: 'pointer',
        width: '100%',
        textAlign: 'left',
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-hover, var(--bg-accent))')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <span style={{ fontSize: 12, color: 'var(--text)', fontWeight: 500 }}>{title}</span>
      <span style={{ fontSize: 11, color: 'var(--muted)' }}>{desc}</span>
    </button>
  )
}

// ---------- card ----------

function ServiceCard({
  svc,
  onOpen,
  onPlayground,
  onOpenWorkflow,
  onDelete,
  onToggleAutostart,
}: {
  svc: ServiceRow
  onOpen: () => void
  onPlayground: () => void
  onOpenWorkflow: (workflowId: string) => void
  onDelete: () => void
  onToggleAutostart: () => void
}) {
  const statusStyle = STATUS_STYLES[svc.status] ?? STATUS_STYLES.active
  const inactive = svc.status !== 'active'
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [menuOpen])

  const copyCurl = (e: React.MouseEvent) => {
    e.stopPropagation()
    const url =
      svc.category === 'llm'
        ? 'https://YOUR_HOST/v1/chat/completions'
        : `https://YOUR_HOST/v1/apps/${svc.name}/run`
    const body =
      svc.category === 'llm'
        ? `{"model": "${svc.name}", "messages": [{"role": "user", "content": "..."}]}`
        : '{}'
    const curl =
      `curl -X POST '${url}' \\\n` +
      `  -H 'Authorization: Bearer YOUR_API_KEY' \\\n` +
      `  -H 'Content-Type: application/json' \\\n` +
      `  -d '${body}'`
    void copyTextOrToast(curl, `已复制 ${svc.name} 的 curl`)
  }

  return (
    <div
      style={{
        background: 'var(--bg-accent)',
        border: '1px solid var(--border)',
        borderLeft: inactive ? '1px solid var(--border)' : '3px solid var(--accent-2, #22c55e)',
        borderRadius: 8,
        padding: 14,
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        opacity: inactive ? 0.7 : 1,
        color: 'var(--text)',
        position: 'relative',
      }}
      // 右键 = 打开同一个菜单(和 ··· 按钮共用 menuOpen 状态,行为一致)。
      onContextMenu={(e) => {
        e.preventDefault()
        setMenuOpen(true)
      }}
    >
      <div ref={menuRef} style={{ position: 'absolute', top: 8, right: 8, zIndex: 2 }}>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            setMenuOpen((p) => !p)
          }}
          title="更多操作"
          style={{
            width: 24,
            height: 24,
            borderRadius: 4,
            border: 'none',
            background: menuOpen ? 'var(--bg)' : 'transparent',
            color: 'var(--muted)',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <MoreHorizontal size={14} />
        </button>
        {menuOpen && (
          <div
            style={{
              position: 'absolute',
              top: 'calc(100% + 4px)',
              right: 0,
              minWidth: 140,
              background: 'var(--bg-elevated, var(--bg))',
              border: '1px solid var(--border)',
              borderRadius: 6,
              boxShadow: '0 8px 24px rgba(0,0,0,0.4)',
              padding: '4px 0',
            }}
          >
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                setMenuOpen(false)
                onToggleAutostart()
              }}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                width: '100%',
                padding: '6px 12px',
                background: 'transparent',
                border: 'none',
                color: 'var(--text)',
                fontSize: 12,
                cursor: 'pointer',
                textAlign: 'left',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'var(--bg-hover, rgba(255,255,255,0.05))'
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent'
              }}
            >
              <Power size={12} />
              {svc.autostart ? '取消开机启动' : '设为开机启动'}
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                setMenuOpen(false)
                onDelete()
              }}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                width: '100%',
                padding: '6px 12px',
                background: 'transparent',
                border: 'none',
                color: 'var(--accent, #ef4444)',
                fontSize: 12,
                cursor: 'pointer',
                textAlign: 'left',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'var(--bg-hover, rgba(255,255,255,0.05))'
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent'
              }}
            >
              <Trash2 size={12} />
              下线
            </button>
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={onOpen}
        style={{
          textAlign: 'left',
          background: 'transparent',
          border: 'none',
          padding: 0,
          cursor: 'pointer',
          color: 'var(--text)',
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
          <CategoryIcon category={effectiveCategory(svc)} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, color: 'var(--text)', fontWeight: 500 }}>{svc.name}</div>
          </div>
          <span
            style={{
              fontSize: 11,
              padding: '2px 7px',
              borderRadius: 10,
              ...statusStyle,
            }}
          >
            {STATUS_LABEL[svc.status] ?? svc.status}
          </span>
        </div>

        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          <CategoryTag category={effectiveCategory(svc)} />
          {svc.source_type === 'comfy_template' && <ComfyBridgeBadge />}
          {svc.autostart && <AutostartBadge />}
          <SourceTag
            sourceType={svc.source_type}
            workflowId={svc.workflow_id}
            workflowName={svc.workflow_name}
            onOpenWorkflow={onOpenWorkflow}
          />
          <Tag>v{svc.version}</Tag>
          <ModelStatusBadge models={svc.models} />
        </div>
        <ServiceCapabilityChips capabilities={svc.capabilities} />

        <div
          style={{
            fontFamily: 'var(--mono, monospace)',
            fontSize: 11,
            color: 'var(--muted)',
            padding: '5px 8px',
            background: 'var(--bg)',
            borderRadius: 4,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {endpointFor(svc)}
        </div>
      </button>

      <div
        style={{
          display: 'flex',
          gap: 6,
          paddingTop: 10,
          borderTop: '1px dashed var(--border)',
        }}
      >
        <CardBtn onClick={onOpen}>详情</CardBtn>
        <CardBtn onClick={onPlayground}>Playground</CardBtn>
        <CardBtn onClick={copyCurl}>复制 curl</CardBtn>
      </div>
    </div>
  )
}

function CardBtn({
  onClick,
  children,
}: {
  onClick: (e: React.MouseEvent) => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        flex: 1,
        padding: '5px 0',
        fontSize: 11,
        textAlign: 'center',
        background: 'var(--bg)',
        color: 'var(--muted)',
        border: '1px solid var(--border)',
        borderRadius: 4,
        cursor: 'pointer',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.color = 'var(--text)'
        e.currentTarget.style.borderColor = 'var(--accent)'
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.color = 'var(--muted)'
        e.currentTarget.style.borderColor = 'var(--border)'
      }}
    >
      {children}
    </button>
  )
}

function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontSize: 10,
        padding: '1px 7px',
        borderRadius: 10,
        background: 'var(--bg)',
        color: 'var(--muted)',
      }}
    >
      {children}
    </span>
  )
}

const CATEGORY_TAG_STYLES: Record<string, React.CSSProperties> = {
  llm: { background: 'rgba(34,197,94,0.15)', color: 'var(--accent-2, #22c55e)' },
  tts: { background: 'rgba(168,85,247,0.15)', color: 'var(--purple, #a855f7)' },
  vl: { background: 'rgba(59,130,246,0.15)', color: 'var(--info, #3b82f6)' },
  image: { background: 'rgba(236,72,153,0.15)', color: 'var(--pink, #ec4899)' },
  asr: { background: 'rgba(14,165,233,0.15)', color: 'var(--sky, #0ea5e9)' },  // 语音识别(2026-06-20)
  app: { background: 'var(--bg)', color: 'var(--muted)' },
}

function CategoryTag({ category }: { category: ServiceCategory | null }) {
  const c = (category ?? 'app').toLowerCase()
  const label = (category ?? 'APP').toString().toUpperCase()
  const style = CATEGORY_TAG_STYLES[c] ?? CATEGORY_TAG_STYLES.app
  return (
    <span style={{ fontSize: 10, padding: '1px 7px', borderRadius: 10, ...style }}>{label}</span>
  )
}

function ComfyBridgeBadge() {
  return (
    <span
      title="通过 ComfyUI 桥导入的工作流服务"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 3,
        fontSize: 10,
        padding: '1px 7px',
        borderRadius: 10,
        background: 'rgba(99,102,241,0.12)',
        color: 'var(--accent)',
        border: '1px solid var(--accent)',
      }}
    >
      <Link2 size={9} />
      桥
    </span>
  )
}

function AutostartBadge() {
  return (
    <span
      title="开机启动:后端启动时会预加载该服务引用的模型"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 3,
        fontSize: 10,
        padding: '1px 7px',
        borderRadius: 10,
        background: 'color-mix(in srgb, var(--warn) 15%, transparent)',
        color: 'var(--warn)',
        border: '1px solid var(--warn)',
      }}
    >
      <Power size={9} />
      开机启动
    </span>
  )
}

function SourceTag({
  sourceType,
  workflowId,
  workflowName,
  onOpenWorkflow,
}: {
  sourceType: 'workflow' | 'preset' | 'model' | 'comfy_template'
  workflowId?: string | null
  workflowName?: string | null
  onOpenWorkflow?: (workflowId: string) => void
}) {
  const isWorkflow = sourceType === 'workflow'
  // 显示 workflow 来源的具体名字 + 短 ID（trivial:xxx 的快速开通命名能让人
  // 一眼看出是真实工作流还是 quick-provision 自动生成的）。comfy_template 已经有
  // 独立的 ComfyBridgeBadge「桥」在旁边标了,这里给个中性描述即可。
  const label = isWorkflow
    ? workflowName
      ? `来自 ${workflowName}${workflowId ? ` #${shortId(workflowId)}` : ''}`
      : '来自 Workflow'
    : sourceType === 'comfy_template'
      ? 'ComfyUI 导入'
      : '快速开通'
  // workflow 来源 + 有 id → 可点击跳到对应 workflow 编辑器。卡片主体是个 <button>,
  // 嵌套 <a> 是非法 DOM,所以用 span + stopPropagation(拦掉卡片自身的 onOpen)。
  const clickable = isWorkflow && !!workflowId && !!onOpenWorkflow
  return (
    <span
      role={clickable ? 'link' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={
        clickable
          ? (e) => {
              e.stopPropagation()
              onOpenWorkflow!(workflowId!)
            }
          : undefined
      }
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                e.stopPropagation()
                onOpenWorkflow!(workflowId!)
              }
            }
          : undefined
      }
      title={
        clickable
          ? `打开 workflow #${workflowId}`
          : isWorkflow && workflowId
            ? `workflow_id=${workflowId}`
            : undefined
      }
      style={{
        fontSize: 10,
        padding: '1px 7px',
        borderRadius: 10,
        background: isWorkflow
          ? 'var(--accent-subtle, rgba(99,102,241,0.1))'
          : 'var(--bg)',
        color: isWorkflow ? 'var(--accent)' : 'var(--muted)',
        border: isWorkflow ? 'none' : '1px solid var(--border)',
        maxWidth: 200,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        display: 'inline-block',
        cursor: clickable ? 'pointer' : undefined,
        textDecoration: clickable ? 'underline dotted' : undefined,
        textUnderlineOffset: clickable ? 2 : undefined,
      }}
    >
      {label}
    </span>
  )
}

function shortId(id: string): string {
  // snowflake ID 是 18-19 位；UI 取末 6 位作短码已足够人眼区分。
  return id.length > 6 ? id.slice(-6) : id
}

function CategoryIcon({ category }: { category: ServiceCategory | null }) {
  const style = { marginTop: 2, color: 'var(--accent)' } as const
  if (category === 'llm') return <Cpu size={16} style={style} />
  if (category === 'vl') return <ImageIcon size={16} style={style} />
  if (category === 'image') return <Images size={16} style={style} />
  if (category === 'app') return <AppWindow size={16} style={style} />
  return <Activity size={16} style={style} />
}

const STATUS_LABEL: Record<string, string> = {
  active: '运行中',
  paused: '已暂停',
  deprecated: '已弃用',
  retired: '已下线',
}
const STATUS_STYLES: Record<string, React.CSSProperties> = {
  active: { background: 'rgba(34,197,94,0.15)', color: 'var(--accent-2, #22c55e)' },
  paused: { background: 'rgba(245,158,11,0.15)', color: 'var(--warn, #f59e0b)' },
  deprecated: { background: 'var(--bg)', color: 'var(--muted)', border: '1px solid var(--border)' },
  retired: { background: 'rgba(239,68,68,0.12)', color: 'var(--error, #ef4444)' },
}

// category 缺失时(旧服务 DB category=NULL,如 quick-provision 前发布的)从 type 推断,
// 否则归类全掉进「其他」、LLM/TTS/VL 计数恒 0。真机实测:qwen3-5-api type=llm 但
// category=null → 显示「LLM 0 · 其他 1」与「全部 2」对不上。type 是后端恒有的字段。
function effectiveCategory(svc: Pick<ServiceRow, 'category' | 'type'>): ServiceCategory {
  if (svc.category) return svc.category
  const t = (svc.type ?? '').toLowerCase()
  if (t === 'llm' || t === 'tts' || t === 'vl') return t
  return 'app'
}

function buildCounts(rows: ServiceRow[]): Record<ServiceCategory, number> {
  const out: Record<ServiceCategory, number> = { llm: 0, tts: 0, vl: 0, app: 0, image: 0, asr: 0, embedding: 0 }
  for (const r of rows) out[effectiveCategory(r)] += 1
  return out
}

function filter(rows: ServiceRow[], tab: FilterTab, search: string): ServiceRow[] {
  const q = search.trim().toLowerCase()
  return rows.filter((r) => {
    if (tab === 'comfy_bridge') {
      if (r.source_type !== 'comfy_template') return false
    } else if (tab !== 'all' && effectiveCategory(r) !== tab) {
      return false
    }
    if (q && !r.name.toLowerCase().includes(q)) return false
    return true
  })
}

function FooterHint() {
  return (
    <p
      style={{
        fontSize: 11,
        color: 'var(--muted)',
        marginTop: 16,
        padding: '10px 14px',
        background: 'var(--bg-accent)',
        borderRadius: 4,
        borderLeft: '2px solid var(--accent-2, #22c55e)',
        lineHeight: 1.7,
      }}
    >
      <strong style={{ color: 'var(--text)' }}>提示：</strong>
      <br />· <strong>快速开通</strong> 适合简单的单步调用（LLM 直接 chat、TTS 直接合成），系统在后台生成 trivial workflow。
      <br />· <strong>从 Workflow 发布</strong> 适合多步流程（LTX 短剧、图像 pipeline），在编辑器里搭好 DAG、指定输入/输出后发布。
      <br />· 两条路径产出同一种服务对象 — 都有 endpoint + schema + 配额 + API Key 授权。
    </p>
  )
}

function Loading() {
  return (
    <div style={{ textAlign: 'center', padding: 40, color: 'var(--muted)', fontSize: 13 }}>
      加载中…
    </div>
  )
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div
      style={{
        background: 'rgba(239,68,68,0.1)',
        border: '1px solid var(--error, #ef4444)',
        color: 'var(--error, #ef4444)',
        padding: 14,
        borderRadius: 6,
        fontSize: 13,
      }}
    >
      {message}
    </div>
  )
}

function Empty({ onCreate }: { onCreate: () => void }) {
  return (
    <div
      style={{
        textAlign: 'center',
        padding: 40,
        background: 'var(--bg-accent)',
        border: '1px dashed var(--border)',
        borderRadius: 8,
        color: 'var(--muted)',
      }}
    >
      <div style={{ fontSize: 14, marginBottom: 6 }}>还没有服务</div>
      <div style={{ fontSize: 12, marginBottom: 12 }}>
        从快速开通开始，或在 Workflow 编辑器里发布一个 DAG。
      </div>
      <button
        type="button"
        onClick={onCreate}
        style={{
          padding: '7px 14px',
          background: 'var(--accent)',
          color: '#fff',
          border: 'none',
          borderRadius: 4,
          fontSize: 12,
          cursor: 'pointer',
        }}
      >
        快速开通
      </button>
    </div>
  )
}
