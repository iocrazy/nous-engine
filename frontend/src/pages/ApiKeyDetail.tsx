import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronUp,
  ClipboardList,
  Copy,
  Eye,
  EyeOff,
  Pause,
  Play,
  Plus,
  RotateCw,
  Search,
  Trash2,
  Unlink,
} from 'lucide-react'
import {
  endpointsFor,
  useAddGrant,
  useApiKey,
  useDeleteApiKey,
  usePatchApiKey,
  useRemoveGrant,
  useResetApiKey,
  useToggleGrant,
  type ApiKeyCreated,
  type GrantSummary,
} from '../api/keys'
import { useServices, type ServiceCapabilities, type ServiceModelRef, type ServiceRow } from '../api/services'
import ModelStatusBadge from '../components/services/ModelStatusBadge'
import { useSettingsStore } from '../stores/settings'
import { copyTextOrToast } from '../utils/clipboard'
import ServiceCapabilityChips from '../components/services/ServiceCapabilityChips'
import { buildClientConfigText } from '../components/services/serviceCapabilities'

// 人话模态标签(按 service_category)——用于「用 OpenAI SDK」那块按模态列服务名。
const MODALITY_LABEL: Record<string, string> = {
  llm: '对话 (chat)',
  embedding: '向量 (embedding)',
  image: '图像 (image)',
  tts: '语音合成 (tts)',
  asr: '语音转写 (asr)',
  vl: '多模态 (vision)',
  understand: '多模态 (vision)',
  app: '应用 (app)',
}

// OpenAI 兼容(/v1/*,用 OpenAI SDK 直连)是主用法;Ollama/Anthropic 只是额外的
// 协议格式 → 折叠成「其它兼容格式」次要区,避免把「一个地址」摊成一堆卡片。
const ALT_PROTOS = new Set(['ollama', 'anthropic'])

export default function ApiKeyDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const apiBaseUrl = useSettingsStore((s) => s.apiBaseUrl)
  const { data: key, isLoading, error } = useApiKey(id ?? null)
  const reset = useResetApiKey()
  const patch = usePatchApiKey()
  const remove = useDeleteApiKey()
  const toggleGrant = useToggleGrant()
  const removeGrant = useRemoveGrant()
  // grant 摘要里没有能力信息 → 按 service_id 从服务列表取(同一份 react-query 缓存,
  // AddGrantPicker 也在用)。
  const { data: services } = useServices()
  const capsByServiceId = useMemo(
    () => new Map((services ?? []).map((s) => [s.id, s.capabilities ?? null])),
    [services],
  )
  const modelsByServiceId = useMemo(
    () => new Map((services ?? []).map((s) => [s.id, s.models])),
    [services],
  )

  const [showSecret, setShowSecret] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [showAltProtos, setShowAltProtos] = useState(false)
  const [resetResult, setResetResult] = useState<ApiKeyCreated | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const [expandedSnippet, setExpandedSnippet] = useState<string | null>(null)

  // 一把 key 可跨模态(综合 key = llm + embedding + ...)。调用方式按授权到的**每个去重
  // 类目各列一组端点**,否则只取首个 grant 会把综合 key 误显成只有 /v1/chat/completions。
  const sampleServices = useMemo(() => {
    const grants = key?.grants ?? []
    const active = grants.filter((g) => g.status === 'active')
    const pool = active.length ? active : grants
    const byCat = new Map<string, GrantSummary>()
    for (const g of pool) {
      const cat = g.service_category ?? 'llm'
      if (!byCat.has(cat)) byCat.set(cat, g)
    }
    return [...byCat.values()]
  }, [key])

  // 按模态分组**所有**授权服务名(不是去重取一个)——「用 OpenAI SDK」那块要把每个
  // 模态下能填进 model 字段的服务名都列出来,免得用户以为只能调示例那一个。
  const modelsByModality = useMemo(() => {
    const grants = (key?.grants ?? []).filter((g) => g.status === 'active')
    const m = new Map<string, string[]>()
    for (const g of grants) {
      const cat = g.service_category ?? 'llm'
      if (!m.has(cat)) m.set(cat, [])
      m.get(cat)!.push(g.service_name)
    }
    return [...m.entries()]
  }, [key])

  // 示例调用地址:UI 与 API 同源(生产 = api.iocrazy.com,dev = localhost:8000)→ 用
  // window.location.origin 才能给出**外部真实可调**的 URL。仅当用户在设置里显式改过
  // apiBaseUrl(非默认 localhost)才尊重其覆盖;否则一律用当前访问域名。
  const exampleBase = useMemo(() => {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000'
    return apiBaseUrl && apiBaseUrl !== 'http://localhost:8000' ? apiBaseUrl : origin
  }, [apiBaseUrl])

  const endpoints = useMemo(() => {
    if (!sampleServices.length) return endpointsFor('<service>', exampleBase, undefined)
    return sampleServices.reduce(
      (acc, g) => Object.assign(acc, endpointsFor(g.service_name, exampleBase, g.service_category)),
      {} as ReturnType<typeof endpointsFor>,
    )
  }, [sampleServices, exampleBase])

  const handleCopy = async (text: string, tag: string) => {
    if (!(await copyTextOrToast(text))) return
    setCopied(tag)
    setTimeout(() => setCopied(null), 1500)
  }

  const handleReset = async () => {
    if (!id) return
    try {
      const out = await reset.mutateAsync(id)
      setResetResult(out)
      setShowSecret(true)
    } finally {
      setConfirmReset(false)
    }
  }

  const handleDelete = async () => {
    if (!id) return
    try {
      await remove.mutateAsync(id)
      navigate('/api-keys')
    } catch {
      setConfirmDelete(false)
    }
  }

  if (isLoading || !key) {
    return (
      <div style={{ padding: 36, color: 'var(--muted)', fontSize: 13 }}>
        {error ? `加载失败：${(error as Error).message}` : '加载中...'}
      </div>
    )
  }

  const visibleSecret: string | null = showSecret
    ? (resetResult?.secret ?? key.secret_plaintext ?? null)
    : null
  const secretDisplay = visibleSecret ?? `${key.key_prefix}${'•'.repeat(20)}`

  const sampleSecret = visibleSecret ?? key.secret_plaintext ?? '<your-api-key>'

  const snippets = sampleServices.length
    ? sampleServices.reduce(
        (acc, g) =>
          Object.assign(acc, buildSnippets(exampleBase, g.service_name, sampleSecret, g.service_category)),
        {} as Record<string, string>,
      )
    : buildSnippets(exampleBase, '<service>', sampleSecret, undefined)

  const renderEndpointCard = (proto: string) => {
    const ep = endpoints[proto]
    if (!ep) return null
    return (
      <div
        key={proto}
        style={{
          border: '1px solid var(--border)',
          borderRadius: 6,
          padding: 12,
          background: 'var(--bg-accent)',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 6,
          }}
        >
          <span style={{ fontSize: 12, color: 'var(--text)', fontWeight: 500 }}>{ep.label}</span>
          <IconBtn title="复制 endpoint" onClick={() => handleCopy(ep.url, `ep-${proto}`)}>
            {copied === `ep-${proto}` ? <Check size={12} /> : <Copy size={12} />}
          </IconBtn>
        </div>
        <div
          style={{
            fontFamily: 'var(--mono, monospace)',
            fontSize: 11,
            color: 'var(--muted)',
            wordBreak: 'break-all',
          }}
        >
          {ep.url}
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>{ep.hint}</div>
        <button
          type="button"
          onClick={() => setExpandedSnippet((p) => (p === proto ? null : proto))}
          style={{
            marginTop: 8,
            padding: '4px 8px',
            background: 'transparent',
            color: 'var(--muted)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            fontSize: 11,
            cursor: 'pointer',
            display: 'inline-flex',
            alignItems: 'center',
            gap: 4,
          }}
        >
          {expandedSnippet === proto ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
          cURL
        </button>
        {expandedSnippet === proto && (
          <pre
            onClick={() => handleCopy(snippets[proto], `snip-${proto}`)}
            style={{
              marginTop: 8,
              padding: 10,
              background: 'var(--bg)',
              border: '1px solid var(--border)',
              borderRadius: 4,
              fontFamily: 'var(--mono, monospace)',
              fontSize: 10,
              color: 'var(--text)',
              whiteSpace: 'pre-wrap',
              cursor: 'pointer',
              margin: '8px 0 0',
              lineHeight: 1.5,
            }}
          >
            {snippets[proto]}
            {copied === `snip-${proto}` && (
              <span style={{ display: 'block', marginTop: 6, color: '#4ade80', fontSize: 9 }}>
                已复制
              </span>
            )}
          </pre>
        )}
      </div>
    )
  }

  const primaryProtos = Object.keys(endpoints).filter((p) => !ALT_PROTOS.has(p))
  const altProtos = Object.keys(endpoints).filter((p) => ALT_PROTOS.has(p))

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        overflow: 'auto',
        background: 'var(--bg)',
      }}
    >
      <div style={{ maxWidth: 1100, margin: '0 auto', padding: 20 }}>
        {/* breadcrumb */}
        <button
          type="button"
          onClick={() => navigate('/api-keys')}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            padding: 0,
            background: 'transparent',
            color: 'var(--muted)',
            border: 'none',
            cursor: 'pointer',
            fontSize: 12,
            marginBottom: 12,
          }}
        >
          <ArrowLeft size={12} /> 返回 API Key 列表
        </button>

        {/* header */}
        <div style={{ display: 'flex', alignItems: 'flex-start', marginBottom: 18 }}>
          <div style={{ flex: 1 }}>
            <h1 style={{ fontSize: 22, fontWeight: 600, color: 'var(--text)' }}>
              {key.label}
            </h1>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4 }}>
              ID #{key.id} · 创建于 {fmtDate(key.created_at)}{' '}
              {key.expires_at ? `· 过期 ${fmtDate(key.expires_at)}` : ''}
            </div>
            {key.note && (
              <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 6 }}>
                {key.note}
              </div>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              type="button"
              onClick={() =>
                patch.mutate({
                  keyId: key.id,
                  body: { is_active: !key.is_active },
                })
              }
              style={btnGhost}
            >
              {key.is_active ? (
                <>
                  <Pause size={12} style={{ marginRight: 4 }} />
                  停用
                </>
              ) : (
                <>
                  <Play size={12} style={{ marginRight: 4 }} />
                  启用
                </>
              )}
            </button>
            <button
              type="button"
              onClick={() => setConfirmReset(true)}
              style={{ ...btnGhost, color: 'var(--accent)', borderColor: 'var(--accent)' }}
            >
              <RotateCw size={12} style={{ marginRight: 4 }} />
              Reset
            </button>
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              style={{ ...btnGhost, color: '#f87171', borderColor: '#f87171' }}
            >
              <Trash2 size={12} style={{ marginRight: 4 }} />
              删除
            </button>
          </div>
        </div>

        {/* secret panel */}
        <Section title="Secret">
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              background: 'var(--bg-accent)',
              padding: '10px 12px',
              borderRadius: 6,
              border: '1px solid var(--border)',
            }}
          >
            <span
              style={{
                fontFamily: 'var(--mono, monospace)',
                fontSize: 13,
                color: visibleSecret ? '#4ade80' : 'var(--text)',
                flex: 1,
                wordBreak: 'break-all',
              }}
            >
              {secretDisplay}
            </span>
            <IconBtn
              title={showSecret ? '隐藏' : '查看明文'}
              onClick={() => setShowSecret((p) => !p)}
              disabled={!key.secret_plaintext && !resetResult}
            >
              {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
            </IconBtn>
            <IconBtn
              title="复制"
              onClick={() => visibleSecret && handleCopy(visibleSecret, 'secret')}
              disabled={!visibleSecret}
            >
              {copied === 'secret' ? <Check size={14} /> : <Copy size={14} />}
            </IconBtn>
          </div>
          {!key.secret_plaintext && !resetResult && (
            <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>
              这是一把旧 key — 创建时未保存明文。点 Reset 一次即可获得可常驻显示的新 secret。
            </div>
          )}
        </Section>

        {/* endpoints */}
        <Section title="调用方式">
          {/* 主用法:一个 Base URL + 一个 key,model 字段切模型(对齐 Doubao/DeepSeek)。 */}
          <div
            style={{
              border: '1px solid var(--border)',
              borderRadius: 8,
              padding: 14,
              background: 'var(--bg-accent)',
              marginBottom: 12,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>Base URL</span>
              <code
                style={{
                  fontFamily: 'var(--mono, monospace)',
                  fontSize: 13,
                  color: 'var(--text)',
                  background: 'var(--bg)',
                  border: '1px solid var(--border)',
                  borderRadius: 4,
                  padding: '3px 8px',
                }}
              >
                {exampleBase}/v1
              </code>
              <IconBtn title="复制 Base URL" onClick={() => handleCopy(`${exampleBase}/v1`, 'baseurl')}>
                {copied === 'baseurl' ? <Check size={12} /> : <Copy size={12} />}
              </IconBtn>
            </div>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
              用 OpenAI 兼容 SDK:<code style={{ fontFamily: 'var(--mono, monospace)' }}>base_url</code>{' '}
              填上面、<code style={{ fontFamily: 'var(--mono, monospace)' }}>api_key</code> 填本 key,
              <code style={{ fontFamily: 'var(--mono, monospace)' }}>model</code> 字段填下面的服务名即可切换。
              子路径(/chat/completions、/embeddings…)由 SDK 自动拼接。
            </div>
            {modelsByModality.length > 0 && (
              <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {modelsByModality.map(([cat, names]) => (
                  <div key={cat} style={{ fontSize: 11, display: 'flex', gap: 8 }}>
                    <span style={{ color: 'var(--muted)', minWidth: 96 }}>
                      {MODALITY_LABEL[cat] ?? cat}
                    </span>
                    <span
                      style={{
                        fontFamily: 'var(--mono, monospace)',
                        color: 'var(--text)',
                        wordBreak: 'break-all',
                      }}
                    >
                      {names.join('  /  ')}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 主端点卡(OpenAI 兼容 /v1/*:chat / embeddings / images …)。 */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
              gap: 10,
            }}
          >
            {primaryProtos.map(renderEndpointCard)}
          </div>

          {/* 次要:Ollama / Anthropic 协议格式,默认折叠。 */}
          {altProtos.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <button
                type="button"
                onClick={() => setShowAltProtos((v) => !v)}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  padding: '6px 10px',
                  background: 'transparent',
                  color: 'var(--muted)',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  fontSize: 12,
                  cursor: 'pointer',
                }}
              >
                {showAltProtos ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                其它兼容格式(Ollama / Anthropic)
              </button>
              {showAltProtos && (
                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
                    gap: 10,
                    marginTop: 10,
                  }}
                >
                  {altProtos.map(renderEndpointCard)}
                </div>
              )}
            </div>
          )}
        </Section>

        {/* grants */}
        <Section title={`授权服务 (${key.grants.length})`}>
          <AddGrantPicker
            keyId={key.id}
            grantedIds={new Set(key.grants.map((g) => g.service_id))}
          />
          {key.grants.length === 0 ? (
            <div
              style={{
                padding: 24,
                textAlign: 'center',
                border: '1px dashed var(--border)',
                borderRadius: 6,
                color: 'var(--muted)',
                fontSize: 12,
              }}
            >
              这把 key 还没授权任何服务。
              <br />
              点上方「+ 授权服务」从已发布服务里添加。
            </div>
          ) : (
            <div
              style={{
                border: '1px solid var(--border)',
                borderRadius: 6,
                overflow: 'hidden',
              }}
            >
              {key.grants.map((g) => (
                <GrantRow
                  key={g.id}
                  g={g}
                  capabilities={capsByServiceId.get(g.service_id) ?? null}
                  models={modelsByServiceId.get(g.service_id)}
                  clientUrl={
                    Object.values(endpointsFor(g.service_name, exampleBase, g.service_category))[0]?.url ?? ''
                  }
                  onToggle={() =>
                    toggleGrant.mutate({
                      grantId: g.id,
                      status: g.status === 'active' ? 'paused' : 'active',
                    })
                  }
                  onRemove={() => removeGrant.mutate(g.id)}
                />
              ))}
            </div>
          )}
        </Section>

        {/* usage */}
        <Section title="用量">
          <div style={{ display: 'flex', gap: 12 }}>
            <Stat label="总调用" value={key.usage_calls.toLocaleString()} />
            <Stat label="字符" value={key.usage_chars.toLocaleString()} />
            <Stat label="最近使用" value={fmtDate(key.last_used_at) ?? '—'} />
          </div>
        </Section>
      </div>

      {confirmReset && (
        <ConfirmDialog
          title="重置 Secret？"
          description="旧 secret 会立即失效（所有用旧 key 的客户端都会 401）。授权关系不变。"
          danger="重置"
          loading={reset.isPending}
          onCancel={() => setConfirmReset(false)}
          onConfirm={handleReset}
        />
      )}
      {confirmDelete && (
        <ConfirmDialog
          title="删除 API Key？"
          description="不可撤销。所有授权同时被回收。"
          danger="删除"
          loading={remove.isPending}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={handleDelete}
        />
      )}
    </div>
  )
}

// ---------- subviews ----------

const CAT_COLOR: Record<string, string> = {
  llm: '#3b82f6', embedding: '#22c55e', image: '#a855f7',
  app: '#f59e0b', vl: '#06b6d4', tts: '#ec4899', asr: '#0ea5e9',
}

/** 「+ 授权服务」:从已发布服务里挑(排除已授权的)给这把 key 加授权。
 *  复用 useAddGrant(POST /keys/{id}/grants);加完 invalidate → 详情刷新、该服务移出可选。 */
function AddGrantPicker({ keyId, grantedIds }: { keyId: string; grantedIds: Set<string> }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const { data: services } = useServices()
  const addGrant = useAddGrant()

  const available = (services ?? []).filter(
    (s) => !grantedIds.has(s.id) &&
      (!q.trim() || s.name.toLowerCase().includes(q.toLowerCase()) ||
        (s.category ?? '').toLowerCase().includes(q.toLowerCase())),
  )

  return (
    <div style={{ marginBottom: 10 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 4, padding: '6px 10px', fontSize: 12,
          background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer',
        }}
      >
        <Plus size={14} /> 授权服务
      </button>
      {open && (
        <div style={{ marginTop: 8, border: '1px solid var(--border)', borderRadius: 6, overflow: 'hidden' }}>
          <div style={{ position: 'relative', padding: 8, borderBottom: '1px solid var(--border)' }}>
            <Search size={13} style={{ position: 'absolute', left: 16, top: '50%', transform: 'translateY(-50%)', color: 'var(--muted)' }} />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索已发布服务…"
              style={{
                width: '100%', boxSizing: 'border-box', background: 'var(--bg-accent)', color: 'var(--text)',
                border: '1px solid var(--border)', borderRadius: 4, padding: '6px 8px 6px 26px', fontSize: 12,
              }}
            />
          </div>
          <div style={{ maxHeight: 280, overflowY: 'auto' }}>
            {available.length === 0 ? (
              <div style={{ padding: 16, textAlign: 'center', color: 'var(--muted)', fontSize: 12 }}>
                没有可加的服务(都授权过了,或没匹配)。
              </div>
            ) : (
              available.map((s: ServiceRow) => (
                <div key={s.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderTop: '1px solid var(--border)' }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: CAT_COLOR[s.category ?? ''] ?? 'var(--muted)', flex: 'none' }} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 13, color: 'var(--text)', fontWeight: 500 }}>{s.name}</div>
                    <div style={{ fontSize: 11, color: 'var(--muted)' }}>
                      {s.category ?? '—'} · {s.source_type === 'workflow' ? (s.workflow_name ?? '工作流') : (s.source_name ?? s.source_type)}
                    </div>
                  </div>
                  <button
                    type="button"
                    disabled={addGrant.isPending}
                    onClick={() => addGrant.mutate({ keyId, serviceId: s.id })}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 3, padding: '5px 10px', fontSize: 12,
                      background: 'transparent', color: 'var(--accent)', border: '1px solid var(--accent)',
                      borderRadius: 4, cursor: addGrant.isPending ? 'wait' : 'pointer', flex: 'none',
                    }}
                  >
                    <Plus size={12} /> 授权
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 22 }}>
      <h2
        style={{
          fontSize: 12,
          color: 'var(--muted)',
          textTransform: 'uppercase',
          letterSpacing: 0.6,
          fontWeight: 600,
          marginBottom: 8,
        }}
      >
        {title}
      </h2>
      {children}
    </div>
  )
}

function GrantRow({
  g,
  capabilities,
  models,
  clientUrl,
  onToggle,
  onRemove,
}: {
  g: GrantSummary
  capabilities: ServiceCapabilities | null
  /** 该服务引用的模型 —— 用来显示**模型加载状态**(与授权状态 active/paused 是两回事) */
  models: ServiceModelRef[] | undefined
  /** 与页面 Base URL 同源(exampleBase + endpointsFor),不硬编码主机名 */
  clientUrl: string
  onToggle: () => void
  onRemove: () => void
}) {
  const [copied, setCopied] = useState(false)
  const copyClientConfig = async () => {
    if (!capabilities) return
    const text = buildClientConfigText({ url: clientUrl, model: g.service_name, caps: capabilities })
    if (!(await copyTextOrToast(text))) return
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '1.6fr 1fr 0.8fr 0.7fr',
        gap: 12,
        padding: '10px 14px',
        borderBottom: '1px solid var(--border)',
        alignItems: 'center',
      }}
    >
      <div>
        <div style={{ fontSize: 13, color: 'var(--text)', fontWeight: 500 }}>
          {g.service_name}
        </div>
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>
          {g.service_category ?? '—'} · grant #{g.id}
        </div>
        <div style={{ marginTop: 4 }}>
          <ServiceCapabilityChips capabilities={capabilities} />
        </div>
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)' }}>
        授权于 {fmtDate(g.activated_at) ?? '—'}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
        {/* 授权状态 ≠ 模型状态(2026-09-23 用户问「active 是什么」):
            这里的 active/paused 只表示这把 key 对该服务的授权生不生效;模型有没有加载看下一行。 */}
        <span
          title={g.status === 'active' ? '这把 key 对该服务的授权生效中' : '授权已暂停:这把 key 调用该服务会被拒'}
          style={{
            fontSize: 11,
            padding: '2px 8px',
            borderRadius: 10,
            background:
              g.status === 'active'
                ? 'rgba(34,197,94,0.12)'
                : 'rgba(248,113,113,0.12)',
            color: g.status === 'active' ? '#4ade80' : '#f87171',
          }}
        >
          {g.status === 'active' ? '授权生效' : '授权暂停'}
        </span>
        <ModelStatusBadge models={models} />
      </div>
      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
        {capabilities && (
          <IconBtn title="复制客户端配置(接口地址 / 模型名 / 工具 / 图片 / 思考 / 上下文 / 提供商)" onClick={copyClientConfig}>
            {copied ? <Check size={12} /> : <ClipboardList size={12} />}
          </IconBtn>
        )}
        <IconBtn title={g.status === 'active' ? '暂停' : '恢复'} onClick={onToggle}>
          {g.status === 'active' ? <Pause size={12} /> : <Play size={12} />}
        </IconBtn>
        <IconBtn title="解除授权" onClick={onRemove}>
          <Unlink size={12} />
        </IconBtn>
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        flex: 1,
        background: 'var(--bg-accent)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '12px 14px',
      }}
    >
      <div style={{ fontSize: 11, color: 'var(--muted)' }}>{label}</div>
      <div
        style={{
          fontSize: 18,
          color: 'var(--text)',
          fontWeight: 600,
          marginTop: 4,
        }}
      >
        {value}
      </div>
    </div>
  )
}

function IconBtn({
  title,
  onClick,
  disabled,
  children,
}: {
  title: string
  onClick: () => void
  disabled?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={(e) => {
        e.stopPropagation()
        if (!disabled) onClick()
      }}
      disabled={disabled}
      style={{
        background: 'transparent',
        border: '1px solid var(--border)',
        color: disabled ? 'var(--muted)' : 'var(--text)',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        borderRadius: 4,
        width: 26,
        height: 26,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      {children}
    </button>
  )
}

function ConfirmDialog({
  title,
  description,
  danger,
  loading,
  onCancel,
  onConfirm,
}: {
  title: string
  description: string
  danger: string
  loading?: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 60,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-elevated, var(--bg))',
          border: '1px solid var(--border)',
          borderRadius: 8,
          width: 380,
          padding: 20,
          boxShadow: '0 20px 50px rgba(0,0,0,0.5)',
        }}
      >
        <h3 style={{ fontSize: 14, color: 'var(--text)', fontWeight: 600 }}>
          {title}
        </h3>
        <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8, lineHeight: 1.6 }}>
          {description}
        </p>
        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 8,
            marginTop: 16,
          }}
        >
          <button onClick={onCancel} type="button" style={btnGhost}>
            取消
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            type="button"
            style={{
              ...btnGhost,
              background: '#ef4444',
              color: '#fff',
              borderColor: '#ef4444',
              cursor: loading ? 'not-allowed' : 'pointer',
              opacity: loading ? 0.6 : 1,
            }}
          >
            {loading ? '...' : danger}
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------- helpers ----------

function fmtDate(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function buildSnippets(
  baseUrl: string,
  model: string,
  apiKey: string,
  category?: string | null,
): Record<string, string> {
  // 按 category 给对应模态的 curl —— 键必须与 endpointsFor 同(渲染遍历共用)。
  if (category === 'embedding') {
    return {
      embeddings: `curl ${baseUrl}/v1/embeddings \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${apiKey}" \\
  -d '{
    "model": "${model}",
    "input": ["第一段文本", "second text"]
  }'`,
    }
  }
  if (category === 'image') {
    return {
      images: `curl ${baseUrl}/v1/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${apiKey}" \\
  -d '{
    "model": "${model}",
    "prompt": "a serene mountain lake at sunrise"
  }'`,
    }
  }
  if (category === 'tts') {
    return {
      audio: `curl ${baseUrl}/v1/audio/speech \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${apiKey}" \\
  -d '{
    "model": "${model}",
    "input": "你好，世界",
    "voice": "default"
  }' --output speech.wav`,
    }
  }
  if (category === 'asr') {
    // ASR 是 multipart 上传(file + model),不是 JSON;键须与 endpointsFor 的 transcriptions 同。
    return {
      transcriptions: `curl ${baseUrl}/v1/audio/transcriptions \\
  -H "Authorization: Bearer ${apiKey}" \\
  -F "file=@audio.wav" \\
  -F "model=${model}"`,
    }
  }
  return {
    openai: `curl ${baseUrl}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${apiKey}" \\
  -d '{
    "model": "${model}",
    "messages": [{"role":"user","content":"hello"}]
  }'`,
    ollama: `curl ${baseUrl}/api/chat \\
  -H "Authorization: Bearer ${apiKey}" \\
  -d '{
    "model": "${model}",
    "messages": [{"role":"user","content":"hello"}]
  }'`,
    anthropic: `curl ${baseUrl}/v1/messages \\
  -H "x-api-key: ${apiKey}" \\
  -H "anthropic-version: 2023-06-01" \\
  -d '{
    "model": "${model}",
    "max_tokens": 1024,
    "messages": [{"role":"user","content":"hello"}]
  }'`,
  }
}

const btnGhost = {
  display: 'inline-flex',
  alignItems: 'center',
  padding: '6px 12px',
  fontSize: 12,
  background: 'transparent',
  color: 'var(--muted)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  cursor: 'pointer',
} as const
