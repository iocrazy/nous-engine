import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useEngines, type EngineInfo } from '../../api/engines'
import { useQuickProvision, NAME_RE, NAME_RULE_HINT, type ServiceCategory } from '../../api/services'

export interface CreateServiceDialogProps {
  open: boolean
  onClose: () => void
  /** Called with the new service's id on success. */
  onCreated?: (id: string) => void
}

const CATEGORIES: { id: ServiceCategory; label: string; help: string }[] = [
  { id: 'llm', label: 'LLM', help: 'OpenAI 兼容 chat/completions' },
  { id: 'tts', label: 'TTS', help: '文本转语音' },
  { id: 'vl', label: 'VL', help: '视觉理解' },
]

export default function CreateServiceDialog({ open, onClose, onCreated }: CreateServiceDialogProps) {
  const [category, setCategory] = useState<ServiceCategory>('llm')
  const [engine, setEngine] = useState('')
  const [name, setName] = useState('')
  const [label, setLabel] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')

  const { data: engines } = useEngines()
  const quickProvision = useQuickProvision()
  const resetMutation = quickProvision.reset

  useEffect(() => {
    if (open) return
    setCategory('llm')
    setEngine('')
    setName('')
    setLabel('')
    setSystemPrompt('')
    resetMutation()
    // resetMutation comes from useMutation; React Query keeps its identity
    // stable, so this effect fires only when `open` flips, not on every render.
  }, [open, resetMutation])

  if (!open) return null

  const filtered = filterEnginesByCategory(engines ?? [], category)
  const nameValid = NAME_RE.test(name)
  const canSubmit = nameValid && !!engine && !quickProvision.isPending

  const submit = async () => {
    if (!canSubmit) return
    const params: Record<string, unknown> = {}
    if (category === 'llm' && systemPrompt.trim()) {
      params.system_prompt = systemPrompt.trim()
    }
    try {
      const svc = await quickProvision.mutateAsync({
        name,
        category,
        engine,
        label: label.trim() || undefined,
        params,
      })
      onCreated?.(svc.id)
      onClose()
    } catch {
      /* error surfaced in UI via mutation state */
    }
  }

  return (
    <Modal onClose={onClose} title="快速开通服务">
      {/* category */}
      <Section label="服务类型">
        <div style={{ display: 'flex', gap: 8 }}>
          {CATEGORIES.map((c) => (
            <button
              key={c.id}
              type="button"
              onClick={() => setCategory(c.id)}
              style={{
                flex: 1,
                padding: '10px 12px',
                borderRadius: 6,
                border: '1px solid',
                borderColor: c.id === category ? 'var(--accent)' : 'var(--border)',
                background: c.id === category ? 'var(--accent-subtle, rgba(99,102,241,0.1))' : 'transparent',
                color: c.id === category ? 'var(--accent)' : 'var(--text)',
                cursor: 'pointer',
                textAlign: 'left',
              }}
            >
              <div style={{ fontWeight: 500, fontSize: 13 }}>{c.label}</div>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>{c.help}</div>
            </button>
          ))}
        </div>
      </Section>

      {/* engine */}
      <Section label="底层引擎">
        <select
          value={engine}
          onChange={(e) => setEngine(e.target.value)}
          style={inputStyle}
        >
          <option value="">— 选一个 {category.toUpperCase()} 引擎 —</option>
          {filtered.map((e) => (
            <option key={e.name} value={e.name}>
              {e.name}
              {e.status === 'loaded' ? ' · 已加载' : ''}
            </option>
          ))}
        </select>
        {filtered.length === 0 && (
          <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>
            没找到匹配 {category.toUpperCase()} 的引擎，先到引擎库里同步一下。
          </div>
        )}
      </Section>

      {/* name + label */}
      <Section label="服务名称 (对外 endpoint key)">
        <input
          value={name}
          onChange={(e) => setName(e.target.value.trim())}
          placeholder="例如：nous-qwen-chat"
          style={inputStyle}
        />
        {name && !nameValid && (
          <div style={{ fontSize: 11, color: 'var(--accent)', marginTop: 6 }}>
            {NAME_RULE_HINT}
          </div>
        )}
      </Section>

      <Section label="显示标签 (可选)">
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="例如：Qwen 3.5 直连"
          style={inputStyle}
        />
      </Section>

      {category === 'llm' && (
        <Section label="System Prompt (可选)">
          <textarea
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            rows={3}
            placeholder="例如：你是一个会写古诗的助手"
            style={{ ...inputStyle, minHeight: 60, resize: 'vertical', fontFamily: 'var(--mono, monospace)' }}
          />
        </Section>
      )}

      {quickProvision.error && (
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
          {(quickProvision.error as Error).message}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 12 }}>
        <button onClick={onClose} type="button" style={btnGhost}>
          取消
        </button>
        <button onClick={submit} disabled={!canSubmit} type="button" style={btnPrimary(canSubmit)}>
          {quickProvision.isPending ? '开通中…' : '开通'}
        </button>
      </div>
    </Modal>
  )
}

function filterEnginesByCategory(engines: EngineInfo[], category: ServiceCategory): EngineInfo[] {
  // Engines carry a `type` like "llm" / "tts"; vl currently shares the llm
  // family. Be permissive: if the engine has no type, show it everywhere.
  return engines.filter((e) => {
    const t = (e as { type?: string }).type?.toLowerCase()
    if (!t) return true
    if (category === 'vl') return t === 'vl' || t === 'llm'
    return t === category
  })
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
          width: 480,
          maxWidth: '92vw',
          maxHeight: '88vh',
          overflow: 'auto',
          padding: 20,
          boxShadow: '0 20px 50px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 16 }}>
          <h2 style={{ flex: 1, fontSize: 16, fontWeight: 600, color: 'var(--text)' }}>{title}</h2>
          <button
            onClick={onClose}
            type="button"
            style={{ background: 'transparent', border: 'none', color: 'var(--muted)', cursor: 'pointer' }}
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
