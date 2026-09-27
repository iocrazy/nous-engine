import { useEffect, useState } from 'react'
import {
  Bell,
  Code2,
  Cpu,
  Database,
  DownloadCloud,
  Gauge,
  GitBranch,
  Info,
  Package,
  Palette,
  ShieldCheck,
  User,
  Zap,
} from 'lucide-react'
import { useServerSettings, useUpdateServerSettings } from '../../api/settings'
import { useSettingsStore, type SettingsState } from '../../stores/settings'
import NodePackagesPanel from '../settings/NodePackagesPanel'
import SecurityPanel from '../settings/SecurityPanel'
import ComfyBridgeSection from '../settings/ComfyBridgeSection'

// m16 v3 mockup 对齐：3 个 sub-nav 分组共 10 项
//   通用：账号 / 外观 / 通知
//   推理：引擎默认 / 节点包 / 限流与配额
//   数据：数据库 / 备份与导出
//   高级：开发者 / 关于
//
// 现有 backend-bound "通用" 内容（路径 / GPU / 服务地址）拆到对应子页：
//   - 引擎默认 = 路径。GPU 归属**不在这里配** —— 每模型覆盖
//     (model_runtime_overrides 表,引擎库右键)才是真相源,没覆盖则
//     GPUAllocator.get_best_gpu 按实时空闲显存自动选卡。
//   - 数据库 = redisUrl + apiBaseUrl
//   其它子页 placeholder（标注"敬请期待"），结构先齐了再逐步接通。

type Section =
  | 'account'
  | 'security'
  | 'appearance'
  | 'notifications'
  | 'engine-defaults'
  | 'packages'
  | 'limits'
  | 'comfy-bridge'
  | 'database'
  | 'backup'
  | 'developer'
  | 'about'

interface NavGroup {
  label: string
  items: { id: Section; label: string; icon: typeof User }[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    label: '通用',
    items: [
      { id: 'account', label: '账号', icon: User },
      { id: 'security', label: '安全', icon: ShieldCheck },
      { id: 'appearance', label: '外观', icon: Palette },
      { id: 'notifications', label: '通知', icon: Bell },
    ],
  },
  {
    label: '推理',
    items: [
      { id: 'engine-defaults', label: '引擎默认', icon: Zap },
      { id: 'packages', label: 'Workflow 节点包', icon: Package },
      { id: 'limits', label: '限流与配额', icon: Gauge },
      { id: 'comfy-bridge', label: 'ComfyUI 桥', icon: GitBranch },
    ],
  },
  {
    label: '数据',
    items: [
      { id: 'database', label: '数据库', icon: Database },
      { id: 'backup', label: '备份与导出', icon: DownloadCloud },
    ],
  },
  {
    label: '高级',
    items: [
      { id: 'developer', label: '开发者', icon: Code2 },
      { id: 'about', label: '关于', icon: Info },
    ],
  },
]

export default function SettingsOverlay() {
  const [section, setSection] = useState<Section>('account')

  return (
    <div className="absolute inset-0 z-[16] flex" style={{ background: 'var(--bg)' }}>
      <SubNav section={section} onChange={setSection} />
      <div style={{ flex: 1, overflowY: 'auto' }}>
        <div style={{ maxWidth: 760, padding: 28, margin: '0 auto' }}>
          <Body section={section} />
        </div>
      </div>
    </div>
  )
}

function SubNav({ section, onChange }: { section: Section; onChange: (s: Section) => void }) {
  return (
    <nav
      style={{
        width: 220,
        borderRight: '1px solid var(--border)',
        background: 'var(--bg-accent)',
        padding: '14px 0',
        flexShrink: 0,
        overflowY: 'auto',
      }}
    >
      {NAV_GROUPS.map((g) => (
        <div key={g.label} style={{ marginBottom: 12 }}>
          <div
            style={{
              fontSize: 10,
              fontWeight: 600,
              color: 'var(--muted)',
              textTransform: 'uppercase',
              letterSpacing: 0.6,
              padding: '6px 16px',
            }}
          >
            {g.label}
          </div>
          {g.items.map(({ id, label, icon: Icon }) => {
            const active = id === section
            return (
              <button
                key={id}
                type="button"
                onClick={() => onChange(id)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  width: '100%',
                  padding: '8px 16px',
                  border: 'none',
                  borderLeft: '2px solid',
                  borderLeftColor: active ? 'var(--accent)' : 'transparent',
                  background: active
                    ? 'var(--accent-subtle, rgba(99,102,241,0.1))'
                    : 'transparent',
                  color: active ? 'var(--text)' : 'var(--muted)',
                  fontSize: 12.5,
                  cursor: 'pointer',
                  textAlign: 'left',
                }}
              >
                <Icon size={13} />
                {label}
              </button>
            )
          })}
        </div>
      ))}
    </nav>
  )
}

function Body({ section }: { section: Section }) {
  switch (section) {
    case 'account':
      return <AccountPanel />
    case 'security':
      return <SecurityPanel />
    case 'appearance':
      return <AppearancePanel />
    case 'notifications':
      return <PlaceholderPanel title="通知" desc="邮件 / Webhook 告警通知。后续接通。" />
    case 'engine-defaults':
      return <EngineDefaultsPanel />
    case 'packages':
      return <NodePackagesPanel />
    case 'limits':
      return <PlaceholderPanel title="限流与配额" desc="全局速率限制、单 key 默认配额规则。后续接通。" />
    case 'comfy-bridge':
      return <ComfyBridgeSection />
    case 'database':
      return <DatabasePanel />
    case 'backup':
      return <PlaceholderPanel title="备份与导出" desc="数据库快照、Workflow / Service 配置导出。后续接通。" />
    case 'developer':
      return <DeveloperPanel />
    case 'about':
      return <AboutPanel />
  }
}

// ---------- 各子页 ----------

function AccountPanel() {
  return (
    <Card>
      <Header title="账号" subtitle="本实例绑定的操作员账号与登录凭据" />
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          padding: '18px 0',
          borderBottom: '1px solid var(--border)',
          marginBottom: 16,
        }}
      >
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: '50%',
            background: 'var(--accent)',
            color: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 22,
            fontWeight: 700,
          }}
        >
          H
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 15, fontWeight: 500, color: 'var(--text)' }}>heygo</div>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            imheygo@gmail.com · 本地管理员
          </div>
        </div>
      </div>

      <KV label="用户名" value="heygo" />
      <KV label="邮箱" value="imheygo@gmail.com" />
      <KV label="部署模式" value="单管理员 · 本地单机" />

      <Note tone="warn">
        nous-engine 当前是单管理员模式，没有访客账户体系。外部调用走 <strong>API Key</strong>（左侧栏）。
      </Note>
    </Card>
  )
}

function AppearancePanel() {
  return (
    <Card>
      <Header title="外观" subtitle="主题切换在左侧 IconRail 底部直接操作" />
      <div style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.7 }}>
        当前控制台支持深色 / 浅色 / 跟随系统三种主题，点 IconRail 左下角太阳/月亮/显示器图标切换。
        UI 主色、字体大小等更细的外观开关计划在后续 PR 接入。
      </div>
    </Card>
  )
}

function EngineDefaultsPanel() {
  return (
    <ServerKVCard
      title="引擎默认"
      subtitle="引擎仓库路径。写入服务端 settings 表"
      fields={[
        {
          key: 'localModelsPath',
          serverKey: 'local_models_path',
          label: '本地模型目录',
          placeholder: '/media/heygo/program/models',
        },
        {
          key: 'cosyvoiceRepoPath',
          serverKey: 'cosyvoice_repo_path',
          label: 'CosyVoice 仓库',
          placeholder: '/path/to/CosyVoice',
        },
        {
          key: 'indexttsRepoPath',
          serverKey: 'indextts_repo_path',
          label: 'IndexTTS 仓库',
          placeholder: '/path/to/index-tts',
        },
      ]}
    />
  )
}

function DatabasePanel() {
  return (
    <ServerKVCard
      title="数据库与缓存"
      subtitle="Redis / 后端 API 地址"
      fields={[
        {
          key: 'redisUrl',
          serverKey: 'redis_url',
          label: 'Redis URL',
          placeholder: 'redis://localhost:6379/0',
        },
        { key: 'apiBaseUrl', label: 'API Base URL', placeholder: 'http://localhost:8000' },
      ]}
    />
  )
}

function DeveloperPanel() {
  return (
    <Card>
      <Header title="开发者" subtitle="调试工具、运行时信息、底层日志开关" />
      <div style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.7 }}>
        待实现：日志级别动态调整、SQL echo 开关、性能 trace。当前请直接看控制台终端输出。
      </div>
      <Note tone="info">
        需要查看请求日志？左侧 IconRail "日志" 入口（m14）已经有完整的请求 / 应用 / 前端 / 审计四档过滤。
      </Note>
    </Card>
  )
}

function AboutPanel() {
  return (
    <Card>
      <Header title="关于" subtitle="" />
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 18 }}>
        <div
          style={{
            width: 48,
            height: 48,
            borderRadius: 8,
            background: 'var(--accent)',
            color: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 22,
            fontWeight: 700,
          }}
        >
          N
        </div>
        <div>
          <div style={{ fontSize: 18, fontWeight: 600, color: 'var(--text)' }}>Nous Engine</div>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
            推理 infra 控制台 · v3 IA
          </div>
        </div>
      </div>

      <SubSection title="定位">
        <p style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.7, margin: 0 }}>
          单管理员的推理 infra 控制台 — 模型 / Workflow / 服务 / API Key 一站管理。Agent /
          Skill 等上层应用由 mediahub 这类消费方实现，nous-engine 只负责把"能调用的服务"
          暴露成稳定 endpoint。
        </p>
      </SubSection>

      <SubSection title="协议兼容">
        <ul
          style={{
            fontSize: 12,
            color: 'var(--text)',
            lineHeight: 1.8,
            paddingLeft: 18,
            margin: 0,
          }}
        >
          <li>
            OpenAI 兼容：<code>/v1/chat/completions</code>
          </li>
          <li>
            Ollama 兼容：<code>/api/chat</code>
          </li>
          <li>
            Anthropic 兼容：<code>/v1/messages</code>
          </li>
        </ul>
      </SubSection>

      <SubSection title="文档与反馈">
        <ul
          style={{
            fontSize: 12,
            color: 'var(--text)',
            lineHeight: 1.8,
            paddingLeft: 18,
            margin: 0,
          }}
        >
          <li>
            设计文档：<code>docs/designs/2026-04-22-ia-rebuild-v3.md</code>
          </li>
          <li>API 文档：在服务详情页 → "API 文档" tab 查看</li>
        </ul>
      </SubSection>
    </Card>
  )
}

function PlaceholderPanel({ title, desc }: { title: string; desc: string }) {
  return (
    <Card>
      <Header title={title} subtitle="敬请期待" />
      <div
        style={{
          padding: 32,
          textAlign: 'center',
          fontSize: 12,
          color: 'var(--muted)',
          border: '1px dashed var(--border)',
          borderRadius: 6,
          lineHeight: 1.7,
        }}
      >
        <Cpu size={20} style={{ opacity: 0.5, marginBottom: 8 }} />
        <div>{desc}</div>
      </div>
    </Card>
  )
}

// ---------- 通用 server-bound KV 卡片 ----------

type SettingsKey = keyof Omit<SettingsState, 'update' | 'reset'>

interface ServerField {
  key: SettingsKey
  serverKey?: string
  label: string
  placeholder?: string
}

function ServerKVCard({
  title,
  subtitle,
  fields,
}: {
  title: string
  subtitle: string
  fields: ServerField[]
}) {
  const store = useSettingsStore()
  const { data: serverSettings } = useServerSettings()
  const updateServer = useUpdateServerSettings()

  const [draft, setDraft] = useState<Record<string, string | number>>(() =>
    Object.fromEntries(fields.map((f) => [f.key, store[f.key]])),
  )
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!serverSettings) return
    const merged: Record<string, string | number> = {}
    for (const f of fields) {
      if (f.serverKey && f.serverKey in serverSettings) {
        merged[f.key] = (serverSettings as unknown as Record<string, string | number>)[f.serverKey]
      }
    }
    setDraft((prev) => ({ ...prev, ...merged }))
    // fields 列表是 caller 提供的常量数组（每个子页声明）；这里只
    // 关心 serverSettings 变化即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSettings])

  const handleSave = () => {
    const localValues: Record<string, string | number> = {}
    for (const f of fields) {
      const v = draft[f.key]
      localValues[f.key] = typeof store[f.key] === 'number' ? Number(v) : v
    }
    store.update(localValues)

    const serverValues: Record<string, string | number> = {}
    for (const f of fields) {
      if (f.serverKey) {
        const v = draft[f.key]
        serverValues[f.serverKey] = typeof store[f.key] === 'number' ? Number(v) : v
      }
    }
    updateServer.mutate(serverValues)

    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  return (
    <Card>
      <Header title={title} subtitle={subtitle} />
      {fields.map((field) => (
        <div key={field.key} style={{ marginBottom: 12 }}>
          <label
            style={{ display: 'block', fontSize: 11, color: 'var(--text)', marginBottom: 4 }}
          >
            {field.label}
          </label>
          <input
            type={typeof store[field.key] === 'number' ? 'number' : 'text'}
            value={draft[field.key] ?? ''}
            placeholder={field.placeholder}
            onChange={(e) => setDraft((prev) => ({ ...prev, [field.key]: e.target.value }))}
            style={{
              width: '100%',
              padding: '6px 9px',
              fontSize: 12,
              fontFamily: 'var(--mono, monospace)',
              background: 'var(--bg-accent)',
              border: '1px solid var(--border)',
              borderRadius: 4,
              color: 'var(--text)',
              outline: 'none',
            }}
          />
        </div>
      ))}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
        <button
          type="button"
          onClick={handleSave}
          disabled={updateServer.isPending}
          style={{
            padding: '7px 16px',
            fontSize: 12,
            borderRadius: 4,
            border: 'none',
            background: 'var(--accent)',
            color: '#fff',
            cursor: updateServer.isPending ? 'wait' : 'pointer',
            opacity: updateServer.isPending ? 0.6 : 1,
          }}
        >
          {updateServer.isPending ? 'Saving...' : '保存'}
        </button>
        {saved && <span style={{ fontSize: 11, color: 'var(--ok)' }}>已保存</span>}
      </div>
    </Card>
  )
}

// ---------- 小通用块 ----------

function Card({ children }: { children: React.ReactNode }) {
  return <div>{children}</div>
}

function Header({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <h2 style={{ fontSize: 16, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
        {title}
      </h2>
      {subtitle && <div style={{ fontSize: 12, color: 'var(--muted)' }}>{subtitle}</div>}
    </div>
  )
}

function SubSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <h3
        style={{
          fontSize: 11,
          fontWeight: 600,
          color: 'var(--muted)',
          textTransform: 'uppercase',
          letterSpacing: 0.5,
          marginBottom: 8,
        }}
      >
        {title}
      </h3>
      {children}
    </div>
  )
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '160px 1fr',
        gap: 16,
        alignItems: 'center',
        padding: '10px 0',
        borderBottom: '1px solid var(--border)',
      }}
    >
      <span style={{ fontSize: 12, color: 'var(--muted)' }}>{label}</span>
      <span style={{ fontSize: 13, color: 'var(--text)' }}>{value}</span>
    </div>
  )
}

function Note({ tone, children }: { tone: 'warn' | 'info'; children: React.ReactNode }) {
  const palette =
    tone === 'warn'
      ? { bg: 'rgba(245,158,11,0.08)', border: 'var(--warn, #f59e0b)' }
      : { bg: 'rgba(59,130,246,0.08)', border: 'var(--info, #3b82f6)' }
  return (
    <div
      style={{
        marginTop: 18,
        padding: '12px 14px',
        background: palette.bg,
        borderLeft: `3px solid ${palette.border}`,
        borderRadius: '0 4px 4px 0',
        fontSize: 12,
        color: 'var(--text)',
        lineHeight: 1.7,
      }}
    >
      {children}
    </div>
  )
}
