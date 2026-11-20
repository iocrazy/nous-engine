import { useEffect } from 'react'
import { AlertTriangle } from 'lucide-react'
import { useConfirmStore } from '../../stores/confirm'

// 统一确认弹窗的单例宿主(挂在 App 根,与 ToastContainer 并列)。
// 视觉对齐 app 既有弹窗(overlay + panel + 头尾);Esc 取消 / Enter 确认 / 点遮罩取消。
export default function ConfirmHost() {
  const current = useConfirmStore((s) => s.current)
  const resolve = useConfirmStore((s) => s.resolve)

  useEffect(() => {
    if (!current) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); resolve(false) }
      else if (e.key === 'Enter') { e.preventDefault(); resolve(true) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [current, resolve])

  if (!current) return null
  const { title, message, confirmText, cancelText, danger } = current

  return (
    <div
      onClick={() => resolve(false)}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', zIndex: 10010,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        style={{
          width: 380, maxWidth: '90vw',
          background: 'var(--bg-elevated, var(--bg))', border: '1px solid var(--border)',
          borderRadius: 'var(--node-radius, 12px)', boxShadow: 'var(--shadow-lg)', overflow: 'hidden',
        }}
      >
        {/* 头部 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '14px 18px 8px' }}>
          {danger && <AlertTriangle size={16} style={{ color: 'var(--accent)', flexShrink: 0 }} />}
          <span style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-strong)' }}>
            {title ?? (danger ? '警告' : '确认')}
          </span>
        </div>
        {/* 正文 */}
        <div style={{ padding: '0 18px 16px', fontSize: 13, lineHeight: 1.6, color: 'var(--text)', whiteSpace: 'pre-wrap' }}>
          {message}
        </div>
        {/* 按钮 */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '12px 18px', borderTop: '1px solid var(--border)' }}>
          <button
            type="button"
            onClick={() => resolve(false)}
            style={{
              padding: '7px 14px', fontSize: 12, borderRadius: 6, cursor: 'pointer',
              background: 'transparent', color: 'var(--muted)', border: '1px solid var(--border)',
            }}
          >
            {cancelText ?? '取消'}
          </button>
          <button
            type="button"
            autoFocus
            onClick={() => resolve(true)}
            style={{
              padding: '7px 14px', fontSize: 12, borderRadius: 6, cursor: 'pointer', fontWeight: 600,
              background: danger ? 'var(--error)' : 'var(--text)',
              color: danger ? 'var(--on-danger)' : 'var(--bg)',
              border: '1px solid transparent',
            }}
          >
            {confirmText ?? '确定'}
          </button>
        </div>
      </div>
    </div>
  )
}
