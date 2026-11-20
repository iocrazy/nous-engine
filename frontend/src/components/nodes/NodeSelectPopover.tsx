/**
 * NodeSelectPopover — 对齐 paperclip `NewIssueDialog` 状态选择器风格的下拉。
 *
 * 替代原生 `<select>`,渲染为「触发按钮 + 浮层列表」:每项 可选彩色点(color) + 标签 + 副标题(description)
 * + 选中高亮(背景 var(--accent-subtle) + 左 Check)。Click-outside / ESC 关闭。
 *
 * 没用 radix/shadcn(nous 风格 = 自定义轻量组件,无新 dep);用 lucide-react Check/ChevronDown +
 * nous CSS 变量贴主题。WidgetDef.options 现支持 {value,label,description?,color?}。
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Check, ChevronDown } from 'lucide-react'

export interface SelectOption {
  value: string
  label: string
  description?: string
  color?: string
  /** 置灰不可选(如 model_select 里「未加载」的模型)。点击无效,样式淡化。 */
  disabled?: boolean
  /** 该项对应的模型/组件是否已在显存。任一 option 带此字段 → 下拉顶部出现「只看已加载」筛选,
   * 且已加载项标绿点。undefined = 无加载语义(dtype/device 等下拉不显示筛选)。 */
  loaded?: boolean
}

export interface NodeSelectPopoverProps {
  value: string
  onChange: (v: string) => void
  options: SelectOption[]
  placeholder?: string
  /** compact = 画布节点内联(更小 font/padding);normal = 属性面板。 */
  size?: 'compact' | 'normal'
  /** 触发按钮宽度,默认 100%。 */
  width?: string | number
  topLayer?: boolean
}

export function NodeSelectPopover({
  value,
  onChange,
  options,
  placeholder,
  size = 'normal',
  width = '100%',
  topLayer = false,
}: NodeSelectPopoverProps) {
  const [open, setOpen] = useState(false)
  const [onlyLoaded, setOnlyLoaded] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const popupRef = useRef<HTMLDivElement>(null)
  const useTopLayer = topLayer && typeof HTMLElement !== 'undefined' && 'showPopover' in HTMLElement.prototype
  const current = options.find((o) => o.value === value)
  // 任一 option 带 loaded 语义 → 显示「只看已加载」筛选(模型/组件下拉);否则不显示(dtype/device)。
  const hasLoadedInfo = options.some((o) => o.loaded !== undefined)
  const loadedCount = options.filter((o) => o.loaded).length
  // 筛选时保留当前选中项(否则选了未加载的会从列表消失,看不到选中态)。
  const shownOptions = onlyLoaded && hasLoadedInfo
    ? options.filter((o) => o.loaded || o.value === value)
    : options

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  useLayoutEffect(() => {
    const popup = popupRef.current
    const anchor = wrapRef.current
    if (!open || !useTopLayer || !popup || !anchor) return

    const rect = anchor.getBoundingClientRect()
    const gap = 4
    const below = window.innerHeight - rect.bottom - gap
    const above = rect.top - gap
    const upward = below < Math.min(280, above)
    popup.style.left = `${rect.left}px`
    popup.style.width = `${rect.width}px`
    popup.style.maxHeight = `${Math.max(0, Math.min(280, upward ? above : below))}px`
    if (upward) popup.style.bottom = `${window.innerHeight - rect.top + gap}px`
    else popup.style.top = `${rect.bottom + gap}px`
    popup.showPopover()

    const onScroll = (event: Event) => {
      if (event.target instanceof Node && popup.contains(event.target)) return
      setOpen(false)
    }
    const onResize = () => setOpen(false)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => {
      if (popup.matches(':popover-open')) popup.hidePopover()
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
    }
  }, [open, useTopLayer])

  const compact = size === 'compact'
  const fs = compact ? 10 : 12
  const triggerPad = compact ? '3px 8px' : '4px 10px'

  return (
    <div ref={wrapRef} className="nodrag" style={{ position: 'relative', width }}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={(e) => {
          e.preventDefault()
          setOpen((o) => !o)
        }}
        style={{
          width: '100%',
          padding: triggerPad,
          fontSize: fs,
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          background: 'var(--bg)',
          border: `1px solid ${open ? 'var(--border-strong)' : 'var(--border)'}`,
          borderRadius: 4,
          color: 'var(--text)',
          fontFamily: 'var(--font)',
          cursor: 'pointer',
          outline: 'none',
          transition: 'border-color 0.15s, background 0.15s',
        }}
        onMouseEnter={(e) => {
          if (!open) e.currentTarget.style.borderColor = 'var(--border-strong)'
        }}
        onMouseLeave={(e) => {
          if (!open) e.currentTarget.style.borderColor = 'var(--border)'
        }}
      >
        {current?.color && (
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: '50%',
              background: current.color,
              flexShrink: 0,
            }}
          />
        )}
        <span
          style={{
            flex: 1,
            textAlign: 'left',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {current?.label ?? placeholder ?? ''}
        </span>
        <ChevronDown size={compact ? 10 : 12} style={{ flexShrink: 0, color: 'var(--muted)' }} />
      </button>

      {open && (
        <div
          ref={popupRef}
          role="listbox"
          popover={useTopLayer ? 'manual' : undefined}
          className="nowheel"
          style={{
            ...(useTopLayer
              ? { position: 'fixed', inset: 'auto', margin: 0 }
              : { position: 'absolute', top: 'calc(100% + 4px)', left: 0, minWidth: '100%', maxHeight: 280 }),

            overflowY: 'auto',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: 6,
            boxShadow: 'var(--shadow-md)',
            padding: 4,
            zIndex: 1000,
          }}
        >
          {/* 「只看已加载」筛选(仅模型/组件下拉有 loaded 语义时显示) */}
          {hasLoadedInfo && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); setOnlyLoaded((v) => !v) }}
              className="nodrag"
              style={{
                width: '100%',
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '5px 8px',
                marginBottom: 4,
                background: onlyLoaded ? 'var(--accent-subtle)' : 'transparent',
                border: '1px solid var(--border)',
                borderRadius: 4,
                cursor: 'pointer',
                color: 'var(--text)',
                fontFamily: 'var(--font)',
                fontSize: fs,
                position: 'sticky',
                top: -4,
              }}
            >
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)', flexShrink: 0 }} />
              <span style={{ flex: 1, textAlign: 'left' }}>只看已加载</span>
              <span style={{ color: 'var(--muted)', fontSize: 10 }}>{loadedCount}</span>
              {onlyLoaded && <Check size={12} style={{ color: 'var(--accent)', flexShrink: 0 }} />}
            </button>
          )}
          {shownOptions.length === 0 && (
            <div style={{ padding: '8px', fontSize: 10, color: 'var(--muted)', textAlign: 'center' }}>
              无已加载项
            </div>
          )}
          {shownOptions.map((opt) => {
            const selected = opt.value === value
            const disabled = !!opt.disabled
            return (
              <button
                key={opt.value}
                type="button"
                role="option"
                aria-selected={selected}
                aria-disabled={disabled}
                disabled={disabled}
                onClick={() => {
                  if (disabled) return
                  onChange(opt.value)
                  setOpen(false)
                }}
                style={{
                  width: '100%',
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 8,
                  padding: '6px 8px',
                  background: selected ? 'var(--accent-subtle)' : 'transparent',
                  border: 'none',
                  borderRadius: 4,
                  cursor: disabled ? 'not-allowed' : 'pointer',
                  opacity: disabled ? 0.45 : 1,
                  textAlign: 'left',
                  color: 'var(--text)',
                  fontFamily: 'var(--font)',
                  transition: 'background 0.1s',
                }}
                onMouseEnter={(e) => {
                  if (!selected && !disabled) e.currentTarget.style.background = 'var(--bg-hover)'
                }}
                onMouseLeave={(e) => {
                  if (!selected) e.currentTarget.style.background = 'transparent'
                }}
              >
                <span
                  style={{
                    width: 14,
                    height: 14,
                    marginTop: 2,
                    flexShrink: 0,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  {selected ? (
                    <Check size={12} style={{ color: 'var(--accent)' }} />
                  ) : opt.loaded ? (
                    <span
                      title="已加载"
                      style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)' }}
                    />
                  ) : opt.color ? (
                    <span
                      style={{ width: 8, height: 8, borderRadius: '50%', background: opt.color }}
                    />
                  ) : null}
                </span>
                <span
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    lineHeight: 1.3,
                    flex: 1,
                    minWidth: 0,
                  }}
                >
                  <span style={{ fontSize: fs, color: 'var(--text)', overflowWrap: 'anywhere' }}>{opt.label}</span>
                  {opt.description && (
                    <span style={{ fontSize: 10, color: 'var(--muted)', marginTop: 1 }}>
                      {opt.description}
                    </span>
                  )}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default NodeSelectPopover
