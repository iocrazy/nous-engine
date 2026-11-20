/**
 * NodeSelectPopover — 对齐 paperclip `NewIssueDialog` 状态选择器风格的下拉。
 *
 * 替代原生 `<select>`,渲染为「触发按钮 + 浮层列表」:每项 可选彩色点(color) + 标签 + 副标题(description)
 * + 选中高亮(背景 var(--accent-subtle) + 左 Check)。Click-outside / ESC 关闭。
 *
 * 没用 radix/shadcn(nous 风格 = 自定义轻量组件,无新 dep);用 lucide-react Check/ChevronDown +
 * nous CSS 变量贴主题。WidgetDef.options 现支持 {value,label,description?,color?}。
 */
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown } from 'lucide-react'
import './NodeSelectPopover.css'

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
  appearance?: 'console'
  ariaLabel?: string
  disabled?: boolean
}

export function NodeSelectPopover({
  value,
  onChange,
  options,
  placeholder,
  size = 'normal',
  width = '100%',
  topLayer = false,
  appearance,
  ariaLabel,
  disabled = false,
}: NodeSelectPopoverProps) {
  const [open, setOpen] = useState(false)
  const [onlyLoaded, setOnlyLoaded] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listId = useId()
  const consoleStyle = appearance === 'console'
  const popupRef = useRef<HTMLDivElement>(null)
  const useTopLayer = topLayer && typeof HTMLElement !== 'undefined' && 'showPopover' in HTMLElement.prototype
  const portal = topLayer && consoleStyle && !useTopLayer
  const floating = useTopLayer || portal
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
      const target = e.target as Node
      if (!wrapRef.current?.contains(target) && !popupRef.current?.contains(target)) setOpen(false)
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
    if (!open || !floating || !popup || !anchor) return

    const rect = anchor.getBoundingClientRect()
    const gap = 4
    const below = window.innerHeight - rect.bottom - gap
    const above = rect.top - gap
    popup.style.left = `${rect.left}px`
    popup.style.width = `${rect.width}px`
    if (useTopLayer) popup.showPopover()
    const upward = below < Math.min(280, popup.scrollHeight) && above > below
    popup.style.maxHeight = `${Math.max(0, Math.min(280, upward ? above : below))}px`
    if (upward) popup.style.bottom = `${window.innerHeight - rect.top + gap}px`
    else popup.style.top = `${rect.bottom + gap}px`

    const onScroll = (event: Event) => {
      if (event.target instanceof Node && popup.contains(event.target)) return
      setOpen(false)
    }
    const onResize = () => setOpen(false)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => {
      if (useTopLayer && popup.matches(':popover-open')) popup.hidePopover()
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
    }
  }, [open, floating, useTopLayer])

  const focusOption = (key: string) => {
    const options = Array.from(popupRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:enabled') ?? [])
    const currentIndex = options.findIndex(option => option === document.activeElement)
    const selectedIndex = options.findIndex(option => option.getAttribute('aria-selected') === 'true')
    const last = options.length - 1
    const index = key === 'Home' ? 0 : key === 'End' ? last
      : currentIndex < 0 ? (selectedIndex >= 0 ? selectedIndex : key === 'ArrowUp' ? last : 0)
      : Math.max(0, Math.min(last, currentIndex + (key === 'ArrowUp' ? -1 : 1)))
    focusVisibleOption(popupRef.current, options[index])
  }

  const onKeyDown = (event: ReactKeyboardEvent) => {
    if (open && event.key === 'Tab') closeAndFocus()
    else if (open && event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      closeAndFocus()
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp' ||
      (open && (event.key === 'Home' || event.key === 'End'))) {
      event.preventDefault()
      event.stopPropagation()
      if (!open) setOpen(true)
      else focusOption(event.key)
    }
  }

  const closeAndFocus = () => {
    setOpen(false)
    triggerRef.current?.focus({ preventScroll: true })
  }

  useLayoutEffect(() => {
    if (open && consoleStyle) {
      const popup = popupRef.current
      const selected = popup?.querySelector<HTMLButtonElement>('[aria-selected="true"]:enabled')
      const first = popup?.querySelector<HTMLButtonElement>('[role="option"]:enabled')
      focusVisibleOption(popup, selected ?? first)
    }
  }, [open, consoleStyle])

  const compact = size === 'compact'
  const fs = compact ? 10 : 12
  const triggerPad = compact ? '3px 8px' : '4px 10px'

  return (
    <div ref={wrapRef} className="nodrag" style={{ position: 'relative', width }}
      onKeyDown={consoleStyle ? onKeyDown : undefined}
      onBlur={consoleStyle && open ? event => {
        if (!wrapRef.current?.contains(event.relatedTarget) && !popupRef.current?.contains(event.relatedTarget)) setOpen(false)
      } : undefined}>
      <button
        ref={triggerRef}
        type="button"
        className={consoleStyle ? 'console-select-trigger' : undefined}
        role={consoleStyle ? 'combobox' : undefined}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-controls={open ? listId : undefined}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={(e) => {
          e.preventDefault()
          setOpen((o) => !o)
        }}
        style={consoleStyle ? undefined : {
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
          if (!consoleStyle && !open) e.currentTarget.style.borderColor = 'var(--border-strong)'
        }}
        onMouseLeave={(e) => {
          if (!consoleStyle && !open) e.currentTarget.style.borderColor = 'var(--border)'
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
        <ChevronDown size={consoleStyle ? 14 : compact ? 10 : 12} style={{ flexShrink: 0, color: 'var(--muted)' }} />
      </button>

      {open && (portal ? createPortal(renderOptions(), document.body) : renderOptions())}
    </div>
  )

  function renderOptions() {
    return (
        <div
          ref={popupRef}
          id={listId}
          role="listbox"
          aria-label={ariaLabel}
          popover={useTopLayer ? 'manual' : undefined}
          className={consoleStyle ? 'nowheel console-select-options' : 'nowheel'}
          style={{
            ...(floating
              ? { position: 'fixed', inset: 'auto', margin: 0 }
              : { position: 'absolute', top: 'calc(100% + 4px)', left: 0, minWidth: '100%', maxHeight: 280 }),

            overflowY: 'auto',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: consoleStyle ? 8 : 6,
            boxShadow: 'var(--shadow-md)',
            padding: consoleStyle ? 5 : 4,
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
                tabIndex={consoleStyle ? -1 : undefined}
                aria-disabled={disabled}
                disabled={disabled}
                onClick={() => {
                  if (disabled) return
                  onChange(opt.value)
                  if (consoleStyle) closeAndFocus()
                  else setOpen(false)
                }}
                style={consoleStyle ? undefined : {
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
                  if (!consoleStyle && !selected && !disabled) e.currentTarget.style.background = 'var(--bg-hover)'
                }}
                onMouseLeave={(e) => {
                  if (!consoleStyle && !selected) e.currentTarget.style.background = 'transparent'
                }}
              >
                {!consoleStyle && <span
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
                </span>}
                <span
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    lineHeight: 1.3,
                    flex: 1,
                    minWidth: 0,
                  }}
                >
                  <span style={{ fontSize: consoleStyle ? undefined : fs, overflowWrap: 'anywhere' }}>{opt.label}</span>
                  {opt.description && (
                    <span style={{ fontSize: 10, color: 'var(--muted)', marginTop: 1 }}>
                      {opt.description}
                    </span>
                  )}
                </span>
                {consoleStyle && selected && <Check size={14} aria-hidden="true" />}
              </button>
            )
          })}
        </div>
    )
  }
}

function focusVisibleOption(popup: HTMLDivElement | null, option?: HTMLButtonElement | null) {
  if (!option || !popup) return
  option.focus({ preventScroll: true })
  const optionTop = option.offsetTop
  const optionBottom = optionTop + option.offsetHeight
  if (optionTop < popup.scrollTop) popup.scrollTop = optionTop
  else if (optionBottom > popup.scrollTop + popup.clientHeight) popup.scrollTop = optionBottom - popup.clientHeight
}

export default NodeSelectPopover
