import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Activity, KeyRound, Plus, Search } from 'lucide-react'
import { useApiKeys, type ApiKeyRow } from '../api/keys'
import CreateApiKeyDialog from '../components/api-keys/CreateApiKeyDialog'

type FilterTab = 'all' | 'active' | 'disabled' | 'unbound'

const TAB_DEFS: { id: FilterTab; label: string; match: (k: ApiKeyRow) => boolean }[] = [
  { id: 'all', label: '全部', match: () => true },
  { id: 'active', label: '已启用', match: (k) => k.is_active },
  { id: 'disabled', label: '已禁用', match: (k) => !k.is_active },
  { id: 'unbound', label: '未授权', match: (k) => k.active_grant_count === 0 },
]

export default function ApiKeysList() {
  const navigate = useNavigate()
  const { data: keys, isLoading, error } = useApiKeys()
  const [tab, setTab] = useState<FilterTab>('all')
  const [search, setSearch] = useState('')
  const [createOpen, setCreateOpen] = useState(false)

  const counts = useMemo(() => {
    const out: Record<FilterTab, number> = { all: 0, active: 0, disabled: 0, unbound: 0 }
    for (const k of keys ?? []) {
      out.all++
      if (k.is_active) out.active++
      else out.disabled++
      if (k.active_grant_count === 0) out.unbound++
    }
    return out
  }, [keys])

  const filtered = useMemo(() => {
    const matcher = TAB_DEFS.find((t) => t.id === tab)?.match ?? (() => true)
    return (keys ?? []).filter((k) => {
      if (!matcher(k)) return false
      if (!search.trim()) return true
      const q = search.toLowerCase()
      return (
        k.label.toLowerCase().includes(q) ||
        k.key_prefix.toLowerCase().includes(q) ||
        (k.note ?? '').toLowerCase().includes(q) ||
        k.grants.some((g) => g.service_name.toLowerCase().includes(q))
      )
    })
  }, [keys, tab, search])

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        overflow: 'auto',
        background: 'var(--bg)',
      }}
    >
      <div className="console-page console-controls">
        {/* header */}
        <div className="console-page-header">
          <div style={{ flex: 1 }}>
            <h1 className="console-title">API Key</h1>
            <p className="console-description">
              一把 Key 可授权 N 个服务 · 明文常驻可复看 · OpenAI / Ollama / Anthropic 三种调用协议
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
                placeholder="搜索 key/服务/备注..."
                style={{
                  width: 260,
                  background: 'var(--bg-accent)',
                  color: 'var(--text)',
                  border: '1px solid var(--border)',
                  borderRadius: 4,
                  padding: '7px 9px 7px 28px',
                  fontSize: 12,
                }}
              />
            </div>
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
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
              新建 Key
            </button>
          </div>
        </div>

        {/* tabs */}
        <div style={{ display: 'flex', gap: 4, marginBottom: 14 }}>
          {TAB_DEFS.map((t) => {
            const active = tab === t.id
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                style={{
                  padding: '6px 12px',
                  background: active
                    ? 'var(--accent-subtle, rgba(99,102,241,0.1))'
                    : 'transparent',
                  color: active ? 'var(--accent)' : 'var(--muted)',
                  border: '1px solid',
                  borderColor: active ? 'var(--accent)' : 'var(--border)',
                  borderRadius: 4,
                  fontSize: 12,
                  cursor: 'pointer',
                }}
              >
                {t.label} {counts[t.id] ?? 0}
              </button>
            )
          })}
        </div>

        {/* body */}
        {isLoading && <Loading />}
        {error && <ErrorBlock message={(error as Error).message} />}
        {keys && keys.length === 0 && <Empty onCreate={() => setCreateOpen(true)} />}
        {filtered.length > 0 && (
          <div
            style={{
              border: '1px solid var(--border)',
              borderRadius: 6,
              overflow: 'hidden',
            }}
          >
            <Header />
            {filtered.map((k) => (
              <Row key={k.id} k={k} onOpen={() => navigate(`/api-keys/${k.id}`)} />
            ))}
          </div>
        )}

        <div
          style={{
            marginTop: 18,
            fontSize: 11,
            color: 'var(--muted)',
            lineHeight: 1.7,
          }}
        >
          创建后请妥善保管你的 secret key — 即使本控制台支持回看，泄漏后仍应立即在详情页 reset。
          <br />
          调用文档见 <code>m11 API 文档</code> 或在 Key 详情页右侧"调用方式"卡片复制示例。
        </div>

        <CreateApiKeyDialog
          open={createOpen}
          onClose={() => setCreateOpen(false)}
          onCreated={(k) => navigate(`/api-keys/${k.id}`)}
        />
      </div>
    </div>
  )
}

// ---------- subviews ----------

function Header() {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '1.4fr 1.6fr 1.5fr 0.7fr 0.9fr 0.9fr',
        gap: 12,
        padding: '8px 14px',
        background: 'var(--bg-accent)',
        borderBottom: '1px solid var(--border)',
        fontSize: 11,
        color: 'var(--muted)',
        textTransform: 'uppercase',
        letterSpacing: 0.5,
      }}
    >
      <div>名称</div>
      <div>Secret · 备注</div>
      <div>授权服务</div>
      <div style={{ textAlign: 'right' }}>调用</div>
      <div>状态</div>
      <div>最近使用</div>
    </div>
  )
}

function Row({ k, onOpen }: { k: ApiKeyRow; onOpen: () => void }) {
  return (
    <div
      onClick={onOpen}
      role="button"
      tabIndex={0}
      style={{
        display: 'grid',
        gridTemplateColumns: '1.4fr 1.6fr 1.5fr 0.7fr 0.9fr 0.9fr',
        gap: 12,
        padding: '12px 14px',
        borderBottom: '1px solid var(--border)',
        cursor: 'pointer',
        background: 'transparent',
        transition: 'background 0.12s',
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-accent)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
    >
      <div>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 13,
            color: 'var(--text)',
            fontWeight: 500,
          }}
        >
          <KeyRound size={13} style={{ color: 'var(--muted)' }} />
          {k.label}
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 3 }}>
          ID #{k.id}
        </div>
      </div>
      <div>
        <div
          style={{
            fontFamily: 'var(--mono, monospace)',
            fontSize: 12,
            color: 'var(--text)',
          }}
        >
          {k.secret_plaintext ?? `${k.key_prefix}...`}
        </div>
        {k.note && (
          <div
            style={{
              fontSize: 11,
              color: 'var(--muted)',
              marginTop: 3,
              maxWidth: 260,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {k.note}
          </div>
        )}
      </div>
      <div>
        {k.grants.length === 0 ? (
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>未授权</span>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {k.grants.slice(0, 4).map((g) => (
              <span
                key={g.id}
                style={{
                  fontSize: 11,
                  color: g.status === 'active' ? 'var(--accent)' : 'var(--muted)',
                  background:
                    g.status === 'active'
                      ? 'var(--accent-subtle, rgba(99,102,241,0.1))'
                      : 'transparent',
                  border: g.status === 'active' ? 'none' : '1px solid var(--border)',
                  padding: '2px 7px',
                  borderRadius: 10,
                }}
              >
                {g.service_name}
              </span>
            ))}
            {k.grants.length > 4 && (
              <span style={{ fontSize: 11, color: 'var(--muted)' }}>
                +{k.grants.length - 4}
              </span>
            )}
          </div>
        )}
      </div>
      <div style={{ textAlign: 'right', fontSize: 12, color: 'var(--text)' }}>
        <Activity size={11} style={{ verticalAlign: 'middle', marginRight: 4, color: 'var(--muted)' }} />
        {k.usage_calls.toLocaleString()}
      </div>
      <div>
        <span
          style={{
            fontSize: 11,
            padding: '2px 8px',
            borderRadius: 10,
            background: k.is_active
              ? 'rgba(34,197,94,0.12)'
              : 'rgba(248,113,113,0.12)',
            color: k.is_active ? '#4ade80' : '#f87171',
          }}
        >
          {k.is_active ? '启用' : '禁用'}
        </span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)' }}>
        {formatRel(k.last_used_at) ?? '—'}
      </div>
    </div>
  )
}

function Loading() {
  return (
    <div style={{ padding: 36, textAlign: 'center', color: 'var(--muted)', fontSize: 12 }}>
      加载中...
    </div>
  )
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div
      style={{
        padding: 14,
        background: 'rgba(239,68,68,0.08)',
        border: '1px solid var(--error, #ef4444)',
        borderRadius: 6,
        color: 'var(--error, #ef4444)',
        fontSize: 12,
      }}
    >
      加载失败：{message}
    </div>
  )
}

function Empty({ onCreate }: { onCreate: () => void }) {
  return (
    <div
      style={{
        padding: 40,
        textAlign: 'center',
        border: '1px dashed var(--border)',
        borderRadius: 6,
        color: 'var(--muted)',
        fontSize: 13,
      }}
    >
      还没有 API Key。
      <br />
      <button
        onClick={onCreate}
        type="button"
        style={{
          marginTop: 12,
          padding: '7px 14px',
          background: 'var(--accent)',
          color: '#fff',
          border: 'none',
          borderRadius: 4,
          fontSize: 12,
          cursor: 'pointer',
        }}
      >
        新建第一把 Key
      </button>
    </div>
  )
}

function formatRel(iso: string | null): string | null {
  if (!iso) return null
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins}分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}小时前`
  const days = Math.floor(hours / 24)
  return `${days}天前`
}
