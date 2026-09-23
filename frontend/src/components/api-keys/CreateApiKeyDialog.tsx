import { useEffect, useMemo, useState } from 'react'
import { Check, Copy, Search, X } from 'lucide-react'
import { useCreateApiKey, type ApiKeyCreated } from '../../api/keys'
import { useServices, type ServiceRow } from '../../api/services'
import { copyTextOrToast } from '../../utils/clipboard'

export interface CreateApiKeyDialogProps {
  open: boolean
  onClose: () => void
  /** 创建成功后返回新 key（带明文）— 调用方决定后续：跳详情/复制等。 */
  onCreated?: (key: ApiKeyCreated) => void
  /** 预选的 service_id（从 m03 "Key 授权" tab 调起时使用）。 */
  // Snowflake 大整数走 string 避开 JS Number 精度（详见 RealAuthTab 注释）。
  preselectedServiceIds?: (number | string)[]
}

export default function CreateApiKeyDialog({
  open,
  onClose,
  onCreated,
  preselectedServiceIds,
}: CreateApiKeyDialogProps) {
  const [label, setLabel] = useState('')
  const [note, setNote] = useState('')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)
  const [copied, setCopied] = useState(false)

  const { data: services } = useServices()
  const createKey = useCreateApiKey()
  const resetMutation = createKey.reset

  const preselectedKey = useMemo(
    () => (preselectedServiceIds ?? []).join(','),
    [preselectedServiceIds],
  )

  useEffect(() => {
    if (open) {
      setSelected(new Set((preselectedServiceIds ?? []).map(String)))
      return
    }
    setLabel('')
    setNote('')
    setSearch('')
    setSelected(new Set())
    setCreated(null)
    setCopied(false)
    resetMutation()
  }, [open, preselectedKey, resetMutation, preselectedServiceIds])

  if (!open) return null

  const filtered = filterServices(services ?? [], search)
  const canSubmit = label.trim().length > 0 && !createKey.isPending

  const toggle = (sid: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(sid)) next.delete(sid)
      else next.add(sid)
      return next
    })
  }

  const submit = async () => {
    if (!canSubmit) return
    try {
      const out = await createKey.mutateAsync({
        label: label.trim(),
        note: note.trim() || undefined,
        service_ids: Array.from(selected),
      })
      setCreated(out)
      onCreated?.(out)
    } catch {
      /* surfaced via mutation state */
    }
  }

  const copySecret = async () => {
    if (!created) return
    if (!(await copyTextOrToast(created.secret))) return
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <Modal onClose={onClose} title={created ? 'API Key 已创建' : '新建 API Key'}>
      {created ? (
        <CreatedView
          created={created}
          copied={copied}
          onCopy={copySecret}
          onClose={onClose}
        />
      ) : (
        <>
          <Section label="Key 名称">
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="例如：mediahub-prod"
              style={inputStyle}
              autoFocus
            />
          </Section>
          <Section label="备注 (可选)">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="给自己看 — 这把 key 给谁/什么用"
              style={inputStyle}
            />
          </Section>

          <Section label={`授权访问 (${selected.size} / ${services?.length ?? 0})`}>
            <div style={{ position: 'relative', marginBottom: 8 }}>
              <Search
                size={12}
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
                style={{ ...inputStyle, paddingLeft: 26 }}
              />
            </div>
            <div
              style={{
                maxHeight: 220,
                overflow: 'auto',
                border: '1px solid var(--border)',
                borderRadius: 4,
              }}
            >
              {filtered.length === 0 && (
                <div style={{ padding: 14, fontSize: 12, color: 'var(--muted)' }}>
                  没有匹配的服务。可以稍后在 Key 详情里追加授权。
                </div>
              )}
              {filtered.map((svc) => {
                const checked = selected.has(svc.id)
                return (
                  <label
                    key={svc.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 10,
                      padding: '8px 10px',
                      cursor: 'pointer',
                      borderBottom: '1px solid var(--border)',
                      background: checked
                        ? 'var(--accent-subtle, rgba(99,102,241,0.08))'
                        : 'transparent',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(svc.id)}
                    />
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 12, color: 'var(--text)' }}>{svc.name}</div>
                      <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 1 }}>
                        {svc.category ?? '—'} · {svc.source_type} · {svc.source_name ?? ''}
                      </div>
                    </div>
                  </label>
                )
              })}
            </div>
          </Section>

          {createKey.error && (
            <div
              style={{
                background: 'rgba(239,68,68,0.1)',
                border: '1px solid var(--error, #ef4444)',
                color: 'var(--error, #ef4444)',
                padding: '8px 10px',
                borderRadius: 4,
                fontSize: 12,
                marginBottom: 12,
              }}
            >
              {(createKey.error as Error).message}
            </div>
          )}

          <div
            style={{
              display: 'flex',
              gap: 8,
              justifyContent: 'flex-end',
              marginTop: 12,
            }}
          >
            <button onClick={onClose} type="button" style={btnGhost}>
              取消
            </button>
            <button
              onClick={submit}
              disabled={!canSubmit}
              type="button"
              style={btnPrimary(canSubmit)}
            >
              {createKey.isPending ? '创建中…' : '创建'}
            </button>
          </div>
        </>
      )}
    </Modal>
  )
}

function CreatedView({
  created,
  copied,
  onCopy,
  onClose,
}: {
  created: ApiKeyCreated
  copied: boolean
  onCopy: () => void
  onClose: () => void
}) {
  return (
    <>
      <div
        style={{
          background: 'rgba(34,197,94,0.08)',
          border: '1px solid rgba(34,197,94,0.3)',
          borderRadius: 6,
          padding: 12,
          marginBottom: 12,
        }}
      >
        <div style={{ fontSize: 11, color: '#4ade80', fontWeight: 600, marginBottom: 6 }}>
          已生成。这把 key 在详情页可以随时回看 + reset，无需立刻保存。
        </div>
        <div
          onClick={onCopy}
          style={{
            display: 'flex',
            gap: 8,
            alignItems: 'center',
            background: 'var(--bg)',
            padding: '8px 10px',
            borderRadius: 4,
            cursor: 'pointer',
            fontFamily: 'var(--mono, monospace)',
            fontSize: 12,
            color: '#4ade80',
            wordBreak: 'break-all',
          }}
        >
          <span style={{ flex: 1 }}>{created.secret}</span>
          {copied ? <Check size={14} /> : <Copy size={14} style={{ color: 'var(--muted)' }} />}
        </div>
      </div>
      <Section label="授权服务">
        {created.grants.length === 0 ? (
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>
            未授权任何服务 — 后续在 Key 详情页追加。
          </div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {created.grants.map((g) => (
              <span
                key={g.id}
                style={{
                  fontSize: 11,
                  color: 'var(--accent)',
                  background: 'var(--accent-subtle, rgba(99,102,241,0.1))',
                  padding: '3px 8px',
                  borderRadius: 10,
                }}
              >
                {g.service_name}
              </span>
            ))}
          </div>
        )}
      </Section>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 12 }}>
        <button onClick={onClose} type="button" style={btnPrimary(true)}>
          完成
        </button>
      </div>
    </>
  )
}

function filterServices(services: ServiceRow[], q: string): ServiceRow[] {
  if (!q.trim()) return services
  const lo = q.toLowerCase()
  return services.filter(
    (s) =>
      s.name.toLowerCase().includes(lo) ||
      (s.source_name ?? '').toLowerCase().includes(lo),
  )
}

function Modal({
  onClose,
  title,
  children,
}: {
  onClose: () => void
  title: string
  children: React.ReactNode
}) {
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 50,
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
          width: 520,
          maxWidth: '92vw',
          maxHeight: '88vh',
          overflow: 'auto',
          padding: 20,
          boxShadow: '0 20px 50px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 16 }}>
          <h2 style={{ flex: 1, fontSize: 16, fontWeight: 600, color: 'var(--text)' }}>
            {title}
          </h2>
          <button
            onClick={onClose}
            type="button"
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--muted)',
              cursor: 'pointer',
            }}
          >
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div
        style={{
          fontSize: 11,
          color: 'var(--muted)',
          textTransform: 'uppercase',
          letterSpacing: 0.5,
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      {children}
    </div>
  )
}

const inputStyle = {
  width: '100%',
  background: 'var(--bg)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '7px 9px',
  fontSize: 12,
} as const

const btnGhost = {
  padding: '7px 14px',
  fontSize: 12,
  background: 'transparent',
  color: 'var(--muted)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  cursor: 'pointer',
} as const

const btnPrimary = (enabled: boolean) =>
  ({
    padding: '7px 14px',
    fontSize: 12,
    background: 'var(--accent)',
    color: '#fff',
    border: 'none',
    borderRadius: 4,
    cursor: enabled ? 'pointer' : 'not-allowed',
    opacity: enabled ? 1 : 0.5,
  }) as const
