import { useState, useMemo, type ChangeEvent, type FormEvent } from 'react'
import { Dices } from 'lucide-react'
import type { ExposedParam } from '../../api/services'
import { paramKey, paramSlot } from '../../api/services'
import NodeSelectPopover from '../nodes/NodeSelectPopover'
import {
  classifyField,
  defaultFor,
  isRandomizable,
  isFieldVisible,
  num,
  optionDependency,
  staticOptions,
  type FieldKind,
} from './fieldKind'
import OptionThumbGrid, { type OptionMeta } from './OptionThumbGrid'
import DependentOptionField from './DependentOptionField'

export interface SchemaDrivenFormProps {
  inputs: ExposedParam[]
  /** Called with `{ key: value }` keyed by `param.key`, ready to POST. */
  onSubmit: (values: Record<string, unknown>) => void
  /** 预填初值(覆盖各字段 default),keyed by exposed key。「重跑(相同参数)」回填用。 */
  initialValues?: Record<string, unknown>
  submitting?: boolean
  submitLabel?: string
  estimateLine?: string
}

function randomSeed(): number {
  // 0..2^31-1 — 安全整数范围内,够用作种子。crypto 优先,回退到时间。
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    return crypto.getRandomValues(new Uint32Array(1))[0] % 2147483647
  }
  return Math.floor(Date.now() % 2147483647)
}

export default function SchemaDrivenForm({
  inputs,
  onSubmit,
  initialValues,
  submitting,
  submitLabel = '▶ 运行',
  estimateLine,
}: SchemaDrivenFormProps) {
  const initial = useMemo(() => {
    const acc: Record<string, unknown> = {}
    for (const p of inputs) {
      const k = paramKey(p)
      if (!k) continue
      // 有预填值用预填(重跑回填),否则用字段 default。
      acc[k] = initialValues && k in initialValues ? initialValues[k] : defaultFor(p)
    }
    return acc
  }, [inputs, initialValues])

  const [values, setValues] = useState<Record<string, unknown>>(initial)

  const update = (k: string, v: unknown) => setValues((prev) => ({ ...prev, [k]: v }))

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    // 留空的**数字**字段要从 payload 里省略,不能发空串 —— 后端
    // validate_service_input 会判 "expected integer" 直接 422,Playground 的运行
    // 按钮对任何带可选空数字字段的服务就此不可用(2026-08-31 实机:krea2 的 seed)。
    // 字符串字段的空串是合法值(用户就是想传空提示词),保留。
    const cleaned: Record<string, unknown> = {}
    for (const p of inputs) {
      const k = paramKey(p)
      if (!k) continue
      const v = values[k]
      if (!isFieldVisible(p, values)) continue
      const kind = classifyField(p)
      const numeric = kind === 'number' || kind === 'integer' || kind === 'slider'
      if (numeric && (v === '' || v === undefined || v === null)) continue
      cleaned[k] = v
    }
    onSubmit(cleaned)
  }

  const reset = () => setValues(initial)

  return (
    <form onSubmit={handleSubmit} className="flex flex-col h-full">
      <div className="flex-1 overflow-auto" style={{ padding: '16px 18px' }}>
        {inputs.length === 0 && (
          <div
            className="text-center"
            style={{ color: 'var(--muted)', fontSize: 12, padding: 24 }}
          >
            该服务没有暴露入参 · 直接点运行即可
          </div>
        )}
        {inputs.filter((p) => isFieldVisible(p, values)).map((p) => {
          // 选项依赖:把「被依赖的那个参数」的当前值传下去(如 style_pack 的值),
          // 字段自己据此拉自己的选项清单。没声明依赖的字段传 undefined,行为不变。
          const dep = optionDependency(p)
          return (
            <Field
              key={`${p.node_id}.${paramKey(p)}`}
              param={p}
              value={values[paramKey(p) ?? '']}
              dependsValue={dep ? String(values[dep.dependsOn] ?? '') : undefined}
              onChange={(v) => {
                const k = paramKey(p)
                if (k) update(k, v)
              }}
            />
          )
        })}
      </div>
      <div
        className="flex items-center gap-2"
        style={{
          padding: '12px 16px',
          borderTop: '1px solid var(--border)',
          background: 'var(--bg-accent)',
        }}
      >
        {estimateLine && (
          <span style={{ flex: 1, fontSize: 11, color: 'var(--muted)' }}>{estimateLine}</span>
        )}
        <button
          type="button"
          onClick={reset}
          className="btn"
          style={{
            fontSize: 12,
            padding: '6px 10px',
            background: 'transparent',
            color: 'var(--muted)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            cursor: 'pointer',
          }}
        >
          清空
        </button>
        <button
          type="submit"
          disabled={submitting}
          style={{
            fontSize: 12,
            padding: '6px 14px',
            background: 'var(--accent)',
            color: '#fff',
            border: 'none',
            borderRadius: 4,
            cursor: submitting ? 'not-allowed' : 'pointer',
            opacity: submitting ? 0.6 : 1,
          }}
        >
          {submitting ? '运行中…' : submitLabel}
        </button>
      </div>
    </form>
  )
}

/** exported: 单字段的「标签 + 类型徽章 + 输入控件」——ComfyTemplateEditor 的
 *  「画布节点预览」左栏(Task 9 UI 返工 Task 3)按同一份 exposedParams 草稿状态实时渲染
 *  控件,直接复用这个而不是另抄一套 classifyField/FieldInput 分支,保证两处的控件外观和
 *  行为(如 seed 🎲 随机、file 类型的 dataURI 上传)不会慢慢漂移。 */
export function Field({
  param,
  value,
  onChange,
  dependsValue,
}: {
  param: ExposedParam
  value: unknown
  onChange: (v: unknown) => void
  /** 该字段的选项所依赖的那个参数的当前值(见 optionDependency)。只有整份表单值
   *  的持有者(SchemaDrivenForm)给得出来;ComfyTemplateEditor 的单字段预览不传,
   *  于是自动退回静态清单。 */
  dependsValue?: string
}) {
  const kind = classifyField(param)
  const slot = paramSlot(param) ?? '?'
  const typeBadge =
    kind === 'string_multiline' ? 'string' : kind === 'slider' ? 'number' : kind === 'radio' ? (param.type || 'string') : kind

  return (
    <div className="flex flex-col gap-2 mb-4">
      <label
        className="flex items-center gap-2"
        title={`node=${param.node_id} · ${slot}`}
      >
        <span style={{ fontSize: 13, color: 'var(--text)', fontWeight: 500 }}>
          {param.label || paramKey(param) || '(unnamed)'}
        </span>
        <span style={{
          fontSize: 10, padding: '1px 6px', borderRadius: 3,
          background: 'var(--bg)', border: '1px solid var(--border)',
          color: 'var(--muted)', fontFamily: 'var(--mono, monospace)',
        }}>
          {typeBadge}
        </span>
        {param.required && (
          <span style={{
            fontSize: 10, color: 'var(--accent)', fontWeight: 500,
          }}>必填</span>
        )}
      </label>
      <FieldInput
        kind={kind}
        param={param}
        value={value}
        onChange={onChange}
        dependsValue={dependsValue}
      />
    </div>
  )
}

function FieldInput({
  kind,
  param,
  value,
  onChange,
  dependsValue,
}: {
  kind: FieldKind
  param: ExposedParam
  value: unknown
  onChange: (v: unknown) => void
  dependsValue?: string
}) {
  const inputStyle = {
    width: '100%',
    background: 'var(--bg)',
    color: 'var(--text)',
    border: '1px solid var(--border)',
    borderRadius: 4,
    padding: '6px 8px',
    fontSize: 12,
  } as const

  if (kind === 'radio') {
    const meta = (param.constraints?.option_meta ?? []) as { value: unknown; label?: string }[]
    const options = (param.constraints?.enum ?? []) as unknown[]
    return (
      <div role="radiogroup" aria-label={param.label || paramKey(param)} style={{ display: 'flex', gap: 12 }}>
        {options.map((option) => (
          <label key={String(option)} style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
            <input
              type="radio"
              name={`${param.node_id}-${paramKey(param)}`}
              checked={value === option}
              onChange={() => onChange(option)}
            />
            {meta.find((item) => item.value === option)?.label ?? String(option)}
          </label>
        ))}
      </div>
    )
  }

  if (kind === 'string_multiline') {
    return (
      <textarea
        value={(value as string) ?? ''}
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => {
          // Auto-grow up to ~12 lines, then scroll
          const ta = e.target
          ta.style.height = 'auto'
          ta.style.height = Math.min(ta.scrollHeight, 264) + 'px'
          onChange(ta.value)
        }}
        ref={(el) => {
          // Initial sizing on mount + when value changes externally
          if (!el) return
          el.style.height = 'auto'
          el.style.height = Math.min(el.scrollHeight, 264) + 'px'
        }}
        rows={3}
        placeholder="支持多行输入"
        style={{
          ...inputStyle,
          padding: 10,
          minHeight: 72,
          resize: 'none',
          overflow: 'auto',
          lineHeight: 1.5,
          fontFamily: 'inherit',
        }}
      />
    )
  }

  // 选项依赖(两种 combo 共用一套逻辑):清单不是注册时冻结的那份,而是按依赖参数
  // 的当前值实时拉。只有真声明了依赖、且拿得到依赖值时才走这条 —— 否则一切照旧。
  const dep = kind === 'thumb_select' || kind === 'select' ? optionDependency(param) : null
  if (dep && dependsValue !== undefined) {
    const c = (param.constraints ?? {}) as { multiple?: unknown }
    return (
      <DependentOptionField
        source={dep.source}
        dependsValue={dependsValue}
        fallback={staticOptions(param)}
        multiple={c.multiple === true}
        value={value}
        onChange={onChange}
      />
    )
  }

  if (kind === 'thumb_select') {
    const c = (param.constraints ?? {}) as { option_meta?: unknown[]; multiple?: unknown }
    return (
      <OptionThumbGrid
        options={(c.option_meta ?? []) as OptionMeta[]}
        value={String(value ?? '')}
        multiple={c.multiple === true}
        onChange={onChange}
      />
    )
  }

  if (kind === 'select') {
    const c = (param.constraints ?? {}) as { enum?: unknown[]; enum_labels?: unknown }
    const opts = (c.enum ?? []) as unknown[]
    // enum_labels: 平行数组或 {value: label} 映射,缺省用 value 本身。
    const labels = c.enum_labels
    // option_meta 优先:桥映射写的是它,enum_labels 是更早的老形态。
    const metaLabels = (param.constraints as Record<string, unknown> | undefined)?.option_meta
    const metaMap = Array.isArray(metaLabels)
      ? Object.fromEntries(
          (metaLabels as OptionMeta[])
            .filter((m) => m?.label)
            .map((m) => [String(m.value), m.label as string]),
        )
      : null
    const labelFor = (o: unknown, i: number): string => {
      if (metaMap && metaMap[String(o)]) return metaMap[String(o)]
      if (Array.isArray(labels)) return String(labels[i] ?? o)
      if (labels && typeof labels === 'object') {
        const m = labels as Record<string, unknown>
        return String(m[String(o)] ?? o)
      }
      return String(o)
    }
    return (
      <NodeSelectPopover
        value={String(value ?? '')}
        onChange={onChange}
        options={opts.map((o, i) => ({ value: String(o), label: labelFor(o, i) }))}
        placeholder="请选择"
      />
    )
  }

  if (kind === 'slider') {
    const c = (param.constraints ?? {}) as Record<string, unknown>
    const min = num(c.min) ?? 0
    const max = num(c.max) ?? 1
    const step = num(c.step) ?? (Number.isInteger(min) && Number.isInteger(max) ? 1 : 0.01)
    const v = num(value) ?? min
    const set = (raw: string) => {
      if (raw === '') return onChange('')
      onChange(Number(raw))
    }
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={v}
          onChange={(e) => set(e.target.value)}
          style={{ flex: 1, accentColor: 'var(--accent)' }}
        />
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          value={(value as number | string) ?? ''}
          onChange={(e) => set(e.target.value)}
          style={{ ...inputStyle, width: 92, flexShrink: 0 }}
        />
      </div>
    )
  }

  if (kind === 'boolean') {
    return (
      <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text)' }}>
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        {value ? 'true' : 'false'}
      </label>
    )
  }

  if (kind === 'number' || kind === 'integer') {
    const numberInput = (
      <input
        type="number"
        step={kind === 'integer' ? 1 : 'any'}
        value={(value as string | number) ?? ''}
        onChange={(e) => {
          const v = e.target.value
          if (v === '') return onChange('')
          onChange(kind === 'integer' ? Number.parseInt(v, 10) : Number.parseFloat(v))
        }}
        style={inputStyle}
      />
    )
    if (!isRandomizable(param)) return numberInput
    // Seed-like field → 数字框 + 🎲 随机按钮。
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <div style={{ flex: 1 }}>{numberInput}</div>
        <button
          type="button"
          title="随机种子"
          aria-label="随机种子"
          onClick={() => onChange(randomSeed())}
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 30, height: 30, flexShrink: 0,
            background: 'var(--bg)', border: '1px solid var(--border)',
            borderRadius: 4, color: 'var(--text)', cursor: 'pointer',
          }}
        >
          <Dices size={15} />
        </button>
      </div>
    )
  }

  if (kind === 'file') {
    // value 存的是 data-URI 字符串(JSON 可序列化);File 对象不能进 JSON body
    // —— 旧实现直接把 File 塞进 values,JSON.stringify 成 {} → 后端收到空图。
    const dataUri = typeof value === 'string' && value.startsWith('data:') ? value : null
    const t = (param.type ?? '').toLowerCase()
    const isImage = t === 'image' || (dataUri?.startsWith('data:image') ?? false)
    const accept = isImage ? 'image/*' : t === 'audio' ? 'audio/*' : t === 'video' ? 'video/*' : undefined
    const onPick = (e: ChangeEvent<HTMLInputElement>) => {
      const f = e.target.files?.[0]
      if (!f) return onChange(null)
      const reader = new FileReader()
      reader.onload = () => onChange(reader.result as string)
      reader.readAsDataURL(f)
    }
    return (
      <label
        style={{
          display: 'block',
          border: '1px dashed var(--border)',
          borderRadius: 4,
          padding: dataUri && isImage ? 8 : 14,
          textAlign: 'center',
          cursor: 'pointer',
          color: 'var(--muted)',
          fontSize: 12,
        }}
      >
        {dataUri && isImage ? (
          <img
            src={dataUri}
            alt="preview"
            style={{ maxWidth: '100%', maxHeight: 180, borderRadius: 3, display: 'block', margin: '0 auto' }}
          />
        ) : dataUri ? (
          '已选文件 · 点击替换'
        ) : (
          '点击或拖入选择文件'
        )}
        <input type="file" accept={accept} hidden onChange={onPick} />
      </label>
    )
  }

  return (
    <input
      type="text"
      value={(value as string) ?? ''}
      onChange={(e) => onChange(e.target.value)}
      style={inputStyle}
    />
  )
}
