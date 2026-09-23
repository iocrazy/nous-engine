import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import type { ExposedParam } from '../../api/services'
import { paramKey, paramSlot } from '../../api/services'
import { copyTextOrToast } from '../../utils/clipboard'

export interface SchemaDrivenOutputProps {
  outputs: ExposedParam[]
  /**
   * Run result. The executor returns `{ node_id: { slot: value, ... } }`,
   * so we pluck via (param.node_id, paramSlot(param)). For services that
   * pre-flatten or return raw text, we also fall back to `result[paramKey]`
   * so older snapshots keep working.
   */
  result: Record<string, unknown> | null
  error?: string | null
}

/**
 * Common slot names a node might emit its primary value on. We try the
 * declared `input_name` first, then fall back through these so the publish
 * default `input_name='value'` still resolves against nodes that actually
 * emit `text` / `output` / `content` / etc.
 */
const SLOT_FALLBACKS = ['text', 'value', 'output', 'content', 'result', 'data'] as const

function pluck(
  result: Record<string, unknown>,
  param: ExposedParam,
): unknown {
  // Backend wraps node outputs as `{ outputs: { node_id: { slot: value } } }`.
  // Older callers may pass the inner dict directly — accept both.
  const root = (result.outputs && typeof result.outputs === 'object'
    ? result.outputs
    : result) as Record<string, unknown>

  const nodeBucket = root[param.node_id]
  if (nodeBucket && typeof nodeBucket === 'object') {
    const bucket = nodeBucket as Record<string, unknown>
    const slot = paramSlot(param)
    if (slot && bucket[slot] !== undefined) return bucket[slot]
    for (const fb of SLOT_FALLBACKS) {
      if (bucket[fb] !== undefined) return bucket[fb]
    }
    // Single-key bucket — use the only value
    const keys = Object.keys(bucket)
    if (keys.length === 1) return bucket[keys[0]]
    return bucket
  }
  // Fallback: flat shape keyed by exposed key (legacy / direct caller)
  const k = paramKey(param)
  if (k && root[k] !== undefined) return root[k]
  return undefined
}

type OutKind = 'audio' | 'video' | 'image' | 'json' | 'text'

function pickMime(p: ExposedParam): OutKind {
  const t = (p.type ?? '').toLowerCase()
  if (t.includes('audio')) return 'audio'
  if (t.includes('video')) return 'video'
  if (t.includes('image')) return 'image'
  const c = (p.constraints ?? {}) as Record<string, unknown>
  const mime = String(c.mime ?? '')
  if (mime.startsWith('audio/')) return 'audio'
  if (mime.startsWith('video/')) return 'video'
  if (mime.startsWith('image/')) return 'image'
  if (t === 'object' || t === 'json') return 'json'
  return 'text'
}

const IMG_EXT = /\.(png|jpe?g|webp|gif|bmp|avif)(\?|#|$)/i
const VIDEO_EXT = /\.(mp4|webm|mov|mkv)(\?|#|$)/i
const AUDIO_EXT = /\.(mp3|wav|ogg|flac|m4a)(\?|#|$)/i

/**
 * 真机发现:image 服务(如 img-flux2)的输出参数常是 type='string' + slot='image_url',
 * pickMime 只看 type → 判成 text → 把图渲成 URL 文本而非 <img>。这里在 type 之外**按值
 * 推断**:URL/data 的扩展名或 image_url 这类名字 → 升级成 image/video/audio。值优先(只在
 * 值确实是 URL 时才用名字提示),避免把普通文本字段错当成坏图。存量服务不用重发布即生效。
 */
function resolveKind(p: ExposedParam, value: unknown): OutKind {
  const declared = pickMime(p)
  if (declared === 'audio' || declared === 'video' || declared === 'image') return declared
  if (typeof value !== 'string') return declared
  const v = value
  if (v.startsWith('data:image') || IMG_EXT.test(v)) return 'image'
  if (v.startsWith('data:video') || VIDEO_EXT.test(v)) return 'video'
  if (v.startsWith('data:audio') || AUDIO_EXT.test(v)) return 'audio'
  const isUrl = v.startsWith('http') || v.startsWith('/') || v.startsWith('data:')
  if (isUrl) {
    const name = `${paramSlot(p) ?? ''} ${paramKey(p) ?? ''}`.toLowerCase()
    if (/image|img|cover|thumb/.test(name)) return 'image'
    if (/audio|voice|speech/.test(name)) return 'audio'
    if (/video|clip/.test(name)) return 'video'
  }
  return declared
}

export default function SchemaDrivenOutput({
  outputs,
  result,
  error,
}: SchemaDrivenOutputProps) {
  if (error) {
    return (
      <div style={{
        background: 'rgba(239,68,68,0.08)',
        border: '1px solid var(--error, #ef4444)',
        color: 'var(--error, #ef4444)',
        padding: '12px 14px',
        borderRadius: 6,
        fontSize: 12,
        fontFamily: 'var(--mono, monospace)',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}>
        {error}
      </div>
    )
  }

  if (!result) {
    return (
      <div style={{
        color: 'var(--muted)', fontSize: 12, padding: '40px 16px',
        textAlign: 'center', border: '1px dashed var(--border)',
        borderRadius: 8,
      }}>
        运行后输出会显示在这里
      </div>
    )
  }

  // No declared outputs — dump raw JSON as fallback
  if (outputs.length === 0) {
    return <JsonBlock value={result} />
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {outputs.map((p) => {
        const k = paramKey(p)
        const v = pluck(result, p)
        if (v === undefined) return null
        return (
          <OutputBlock
            key={`${p.node_id}.${k}`}
            label={p.label || k || 'output'}
            kind={resolveKind(p, v)}
            value={v}
            singular={outputs.length === 1}
          />
        )
      })}
    </div>
  )
}

function OutputBlock({
  label,
  kind,
  value,
  singular,
}: {
  label: string
  kind: 'audio' | 'video' | 'image' | 'json' | 'text'
  value: unknown
  singular: boolean
}) {
  return (
    <div>
      {!singular && (
        <div style={{
          fontSize: 11, color: 'var(--muted)',
          textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6,
        }}>
          {label}
        </div>
      )}
      <Renderer kind={kind} value={value} />
    </div>
  )
}

function asUrlOrText(value: unknown): { kind: 'url' | 'text'; value: string } {
  if (typeof value === 'string') {
    if (
      value.startsWith('http://') ||
      value.startsWith('https://') ||
      value.startsWith('data:') ||
      value.startsWith('/')
    ) {
      return { kind: 'url', value }
    }
    return { kind: 'text', value }
  }
  return { kind: 'text', value: JSON.stringify(value) }
}

function Renderer({
  kind,
  value,
}: {
  kind: 'audio' | 'video' | 'image' | 'json' | 'text'
  value: unknown
}) {
  if (kind === 'audio') {
    const u = asUrlOrText(value)
    if (u.kind === 'url') return <audio controls src={u.value} style={{ width: '100%' }} />
    return <Mono>{u.value}</Mono>
  }
  if (kind === 'video') {
    const u = asUrlOrText(value)
    if (u.kind === 'url') return <video controls src={u.value} style={{ width: '100%', borderRadius: 4 }} />
    return <Mono>{u.value}</Mono>
  }
  if (kind === 'image') {
    const u = asUrlOrText(value)
    if (u.kind === 'url') return <img src={u.value} alt="" style={{ width: '100%', borderRadius: 4 }} />
    return <Mono>{u.value}</Mono>
  }
  if (kind === 'json') {
    return <JsonBlock value={value} />
  }
  // text
  if (typeof value === 'string') {
    return <TextBlock value={value} />
  }
  return <JsonBlock value={value} />
}

function TextBlock({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    if (!(await copyTextOrToast(value))) return
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={copy}
        title="复制"
        style={{
          position: 'absolute', top: 8, right: 8,
          padding: '4px 8px', fontSize: 11,
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 4, color: copied ? 'var(--ok, #34c759)' : 'var(--muted)',
          cursor: 'pointer',
          display: 'inline-flex', alignItems: 'center', gap: 4,
          transition: 'color 0.12s',
        }}
      >
        {copied ? <Check size={11} /> : <Copy size={11} />}
        {copied ? '已复制' : '复制'}
      </button>
      <div style={{
        background: 'var(--bg)', border: '1px solid var(--border)',
        borderRadius: 6, padding: '14px 16px',
        fontSize: 14, color: 'var(--text)', lineHeight: 1.6,
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        paddingRight: 80,
      }}>
        {value}
      </div>
    </div>
  )
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre style={{
      background: 'var(--bg)', border: '1px solid var(--border)',
      borderRadius: 6, padding: 12, margin: 0,
      fontSize: 11, fontFamily: 'var(--mono, monospace)',
      color: 'var(--text)', overflow: 'auto',
    }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

function Mono({ children }: { children: React.ReactNode }) {
  return (
    <code style={{
      background: 'var(--bg)', border: '1px solid var(--border)',
      borderRadius: 4, padding: '2px 6px',
      fontSize: 11, fontFamily: 'var(--mono, monospace)',
      color: 'var(--text)', wordBreak: 'break-all',
    }}>
      {children}
    </code>
  )
}
