import { useMemo, useState } from 'react'
import { Check, Search, X } from 'lucide-react'
import { parseValues } from './fieldKind'

/** 一个带元数据的 combo 选项。后端 `constraints.option_meta` 的项形
 *  (comfy_templates.py::_split_options 写入),`label`/`image` 都可缺省。 */
export interface OptionMeta {
  value: string
  label?: string
  image?: string
}

export interface OptionThumbGridProps {
  options: OptionMeta[]
  /** 当前值。多选时是**逗号分隔字符串** —— 与 ComfyUI-Easy-Use 的 select_styles
   *  传输形态一致(prompt.py:196 `select_styles.split(',')`)。 */
  value: string
  multiple?: boolean
  compact?: boolean
  onChange: (next: string) => void
}

/**
 * 缩略图网格选择器 —— 275 个 fooocus 风格用纯文本下拉根本没法挑,这里按 ComfyUI
 * 侧 `easy stylesSelector` 的交互还原:搜索 + 图卡 + 多选。
 *
 * 只在选项**真带缩略图**时才由 SchemaDrivenForm 选用;没有 image 的选项照常渲成
 * 文字卡(不留空白占位),全都没有 image 时上游会退回原来的 <select>。
 */
export default function OptionThumbGrid({
  options,
  value,
  multiple = false,
  compact = false,
  onChange,
}: OptionThumbGridProps) {
  const [q, setQ] = useState('')

  const selected = useMemo(
    () => new Set(multiple ? parseValues(value) : value ? [value] : []),
    [value, multiple],
  )

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return options
    // 值和显示名都参与匹配 —— 用户可能记得 "sai-anime" 也可能记得 "SAI-动漫"。
    return options.filter(
      (o) =>
        o.value.toLowerCase().includes(needle) ||
        (o.label ?? '').toLowerCase().includes(needle),
    )
  }, [options, q])

  function toggle(v: string) {
    if (!multiple) {
      // 单选:再点一次已选项 = 取消(否则必填为空时没法清掉)
      onChange(!compact && selected.has(v) ? '' : v)
      return
    }
    const next = new Set(selected)
    if (next.has(v)) next.delete(v)
    else next.add(v)
    // 保持 options 原序输出,而不是点击顺序 —— 风格叠加的先后会影响提示词拼接结果,
    // 用同一套顺序才能让「选了同样几个」得到同样的图。
    onChange(options.filter((o) => next.has(o.value)).map((o) => o.value).join(','))
  }

  return (
    <div>
      {!compact && <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <div style={{ position: 'relative', flex: 1 }}>
          <Search
            size={13}
            style={{
              position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)',
              color: 'var(--muted)', pointerEvents: 'none',
            }}
          />
          <input
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={`搜索 ${options.length} 个选项…`}
            aria-label="搜索选项"
            style={{
              width: '100%', padding: '5px 8px 5px 26px', fontSize: 12,
              background: 'var(--bg)', color: 'var(--text)',
              border: '1px solid var(--border)', borderRadius: 5,
            }}
          />
        </div>
        {selected.size > 0 && (
          <button
            type="button"
            onClick={() => onChange('')}
            title="清空选择"
            style={{
              display: 'flex', alignItems: 'center', gap: 4, fontSize: 11,
              padding: '4px 8px', color: 'var(--muted)', background: 'transparent',
              border: '1px solid var(--border)', borderRadius: 5, cursor: 'pointer',
            }}
          >
            <X size={11} />
            已选 {selected.size}
          </button>
        )}
      </div>}

      {shown.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--muted)', padding: '12px 0' }}>
          没有匹配「{q}」的选项
        </div>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: compact ? 'repeat(3, minmax(0, 1fr))' : 'repeat(auto-fill, minmax(88px, 1fr))',
            gap: 8,
            maxHeight: 320,
            overflowY: 'auto',
            padding: 2,
          }}
        >
          {shown.map((o) => {
            const on = selected.has(o.value)
            return (
              <button
                key={o.value}
                type="button"
                onClick={() => toggle(o.value)}
                aria-pressed={on}
                title={compact ? o.label : o.value}
                style={{
                  display: compact ? 'block' : undefined,
                  position: 'relative', padding: 0, cursor: 'pointer', overflow: 'hidden',
                  background: on ? 'var(--bg-accent)' : 'var(--bg)',
                  border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
                  borderRadius: 6, textAlign: 'left',
                }}
              >
                {o.image ? (
                  <img
                    src={o.image}
                    alt=""
                    loading="lazy"
                    style={{ width: '100%', aspectRatio: '1', objectFit: compact ? 'contain' : 'cover', display: 'block' }}
                  />
                ) : (
                  // 没有缩略图的项不留空白占位,直接给一块纯文字卡
                  <div
                    style={{
                      width: '100%', aspectRatio: '1', display: 'flex',
                      alignItems: 'center', justifyContent: 'center',
                      fontSize: 10, color: 'var(--muted)', padding: 4, textAlign: 'center',
                    }}
                  >
                    {o.label ?? o.value}
                  </div>
                )}
                <div
                  style={{
                    fontSize: 10, lineHeight: 1.3, padding: '4px 5px',
                    color: on ? 'var(--text)' : 'var(--muted)',
                    whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                  }}
                >
                  {o.label ?? o.value}
                </div>
                {on && (
                  <Check
                    size={12}
                    style={{
                      position: 'absolute', top: 4, right: 4, color: 'var(--accent)',
                      background: 'var(--bg)', borderRadius: 3,
                    }}
                  />
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
