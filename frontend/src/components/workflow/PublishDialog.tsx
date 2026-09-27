import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { X, ChevronLeft, ChevronRight } from 'lucide-react'
import {
  NAME_RE,
  NAME_RULE_HINT,
  usePublishWorkflow,
  type ExposedParam,
  type ServiceCategory,
} from '../../api/services'
import WorkflowAppEditor, { type AppEditorValue } from './WorkflowAppEditor'
import type { EditorNodeLike } from './appEditorSchema'

export interface PublishDialogProps {
  open: boolean
  onClose: () => void
  workflowId: string | number
  /** Editor-style nodes ([{id, type, data, position}, …]) — the user ticks
   *  which node widgets become the public form (per-widget, not per-node). */
  nodes: Array<{
    id: string
    type?: string
    data?: Record<string, unknown>
    position?: { x: number; y: number }
  }>
  edges?: Array<{ source: string; target: string }>
  onPublished?: (serviceId: string) => void
}

type Step = 'expose' | 'name'

const OUT_RE = /output|save|preview/i

/** First-run defaults: auto-tick output-ish nodes as outputs; inputs start
 *  empty (user ticks the widgets they want exposed on the canvas). */
function defaultValue(nodes: PublishDialogProps['nodes']): AppEditorValue {
  const outputs: ExposedParam[] = nodes
    .filter((n) => OUT_RE.test(n.type ?? ''))
    .map((n, i) => {
      const slot = /text/i.test(n.type ?? '') ? 'text' : 'image_url'
      return {
        node_id: n.id,
        key: `output_${i + 1}`,
        input_name: slot,
        label: n.type ?? 'output',
        type: slot === 'image_url' ? 'image' : 'string',
      }
    })
  return { inputs: [], outputs }
}

export default function PublishDialog({
  open,
  onClose,
  workflowId,
  nodes,
  edges = [],
  onPublished,
}: PublishDialogProps) {
  const [step, setStep] = useState<Step>('expose')
  const [value, setValue] = useState<AppEditorValue>({ inputs: [], outputs: [] })
  const [name, setName] = useState('')
  const [label, setLabel] = useState('')
  const [category, setCategory] = useState<ServiceCategory>('app')

  const publish = usePublishWorkflow()
  const resetMutation = publish.reset

  // close → wipe state. Depend ONLY on [open, resetMutation] (resetMutation is
  // stable) — adding `nodes` here re-fired the effect every render the parent
  // passed a fresh literal and used to feed an infinite loop.
  useEffect(() => {
    if (open) return
    setStep('expose')
    setValue({ inputs: [], outputs: [] })
    setName('')
    setLabel('')
    setCategory('app')
    resetMutation()
  }, [open, resetMutation])

  // open → seed default exposed selection. Depend on a shallow node-id key so a
  // fresh `nodes` literal each render doesn't re-seed (and clobber edits).
  const nodesKey = nodes.map((n) => n.id).join('|')
  useEffect(() => {
    if (!open || nodes.length === 0) return
    setValue(defaultValue(nodes))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, nodesKey])

  const editorNodes = useMemo<EditorNodeLike[]>(
    () => nodes.map((n) => ({ id: n.id, type: n.type ?? '', data: n.data, position: n.position })),
    [nodes],
  )

  if (!open) return null

  const nameValid = NAME_RE.test(name)
  const exposedCount = value.inputs.length + value.outputs.length
  const canSubmit = nameValid && exposedCount > 0

  const submit = async () => {
    if (!canSubmit) return
    try {
      const svc = await publish.mutateAsync({
        workflowId,
        body: {
          name,
          label: label.trim() || undefined,
          category,
          exposed_inputs: value.inputs,
          exposed_outputs: value.outputs,
        },
      })
      onPublished?.(svc.id)
      onClose()
    } catch {
      /* surfaced in UI */
    }
  }

  return createPortal(
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
        backdropFilter: 'blur(4px)', WebkitBackdropFilter: 'blur(4px)',
        zIndex: 50, display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-elevated, var(--bg))',
          border: '1px solid var(--border)', borderRadius: 8,
          width: step === 'expose' ? '92vw' : 520,
          height: step === 'expose' ? '86vh' : 'auto',
          maxWidth: step === 'expose' ? 1280 : '92vw',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
          boxShadow: '0 20px 60px rgba(0,0,0,0.55)',
        }}
      >
        {/* header */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '14px 18px', borderBottom: '1px solid var(--border)',
        }}>
          <h2 style={{ flex: 1, fontSize: 16, fontWeight: 600, color: 'var(--text)' }}>发布为服务</h2>
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>
            {step === 'expose'
              ? `已暴露 ${exposedCount} 字段(入参 ${value.inputs.length} · 出参 ${value.outputs.length})`
              : '命名并发布'}
          </span>
          <button onClick={onClose} type="button"
            style={{ background: 'transparent', border: 'none', color: 'var(--muted)', cursor: 'pointer' }}>
            <X size={18} />
          </button>
        </div>

        {/* body */}
        {step === 'expose' ? (
          editorNodes.length === 0 ? (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--muted)', fontSize: 13 }}>
              这个 workflow 还没有节点 · 先到画布加节点
            </div>
          ) : (
            <div style={{ flex: 1, minHeight: 0 }}>
              <WorkflowAppEditor
                nodes={editorNodes}
                edges={edges}
                value={value}
                onChange={setValue}
                runnable={false}
              />
            </div>
          )
        ) : (
          <div style={{ padding: 20, overflow: 'auto' }}>
            <Section label="服务名称">
              <input value={name} onChange={(e) => setName(e.target.value.trim())}
                placeholder="例如:nous-ltx-drama" style={inputStyle} />
              {name && !nameValid && (
                <div style={{ fontSize: 11, color: 'var(--accent)', marginTop: 6 }}>
                  {NAME_RULE_HINT}
                </div>
              )}
            </Section>
            <Section label="显示标签 (可选)">
              <input value={label} onChange={(e) => setLabel(e.target.value)} style={inputStyle} />
            </Section>
            <Section label="分类">
              <select value={category} onChange={(e) => setCategory(e.target.value as ServiceCategory)}
                style={inputStyle}>
                <option value="app">通用 (app)</option>
                <option value="llm">LLM</option>
                <option value="tts">TTS</option>
                <option value="vl">VL</option>
                <option value="image">图像</option>
              </select>
            </Section>
            <div style={{
              marginTop: 8, padding: '10px 12px', background: 'var(--bg)',
              border: '1px solid var(--border)', borderRadius: 4, fontSize: 11,
              color: 'var(--muted)', fontFamily: 'var(--mono, monospace)',
            }}>
              workflow_id: {String(workflowId)} · 入参 {value.inputs.length} · 出参 {value.outputs.length}
            </div>
          </div>
        )}

        {publish.error && (
          <div style={{
            background: 'rgba(239,68,68,0.1)', border: '1px solid var(--error, #ef4444)',
            color: 'var(--error, #ef4444)', padding: '8px 10px', borderRadius: 4,
            fontSize: 12, margin: '0 18px 12px',
          }}>
            {(publish.error as Error).message}
          </div>
        )}

        {/* footer */}
        <div style={{
          display: 'flex', gap: 8, justifyContent: 'space-between',
          padding: '12px 18px', borderTop: '1px solid var(--border)',
        }}>
          <button type="button" onClick={() => setStep('expose')}
            disabled={step === 'expose'}
            style={{ ...btnGhost, opacity: step === 'expose' ? 0.4 : 1 }}>
            <ChevronLeft size={14} style={{ marginRight: 4, verticalAlign: 'middle' }} />
            上一步
          </button>
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={onClose} type="button" style={btnGhost}>取消</button>
            {step === 'expose' ? (
              <button type="button" onClick={() => setStep('name')}
                disabled={exposedCount === 0}
                style={btnPrimary(exposedCount > 0)}>
                下一步
                <ChevronRight size={14} style={{ marginLeft: 4, verticalAlign: 'middle' }} />
              </button>
            ) : (
              <button type="button" onClick={submit}
                disabled={!canSubmit || publish.isPending}
                style={btnPrimary(canSubmit && !publish.isPending)}>
                {publish.isPending ? '发布中…' : '发布服务'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{
        fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
        letterSpacing: 0.5, marginBottom: 6,
      }}>
        {label}
      </div>
      {children}
    </div>
  )
}

const inputStyle = {
  width: '100%', background: 'var(--bg)', color: 'var(--text)',
  border: '1px solid var(--border)', borderRadius: 4, padding: '7px 9px', fontSize: 12,
} as const

const btnGhost = {
  padding: '7px 14px', fontSize: 12, background: 'transparent', color: 'var(--muted)',
  border: '1px solid var(--border)', borderRadius: 4, cursor: 'pointer',
} as const

const btnPrimary = (enabled: boolean) =>
  ({
    padding: '7px 14px', fontSize: 12, background: 'var(--accent)', color: '#fff',
    border: 'none', borderRadius: 4, cursor: enabled ? 'pointer' : 'not-allowed',
    opacity: enabled ? 1 : 0.5,
  }) as const
