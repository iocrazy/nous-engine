import { useToastStore } from '../stores/toast'

/**
 * 复制文本到剪贴板,返回是否成功;永不抛。
 *
 * 生产经明文 HTTP 内网访问(`http://heygo-ubuntu:8000`,见 CLAUDE.md「别加 HSTS」),
 * 浏览器只把 https / localhost 当安全上下文,此时 `navigator.clipboard` 是 undefined。
 * 所以非安全上下文走老的 `<textarea>` + `execCommand('copy')` 回退 —— 它已被标记
 * deprecated,但在非安全上下文里是唯一可用的写剪贴板手段。
 */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 权限被拒 / 文档不在焦点等 —— 落到下面的回退再试一次
    }
  }
  return execCommandCopy(text)
}

function execCommandCopy(text: string): boolean {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.top = '-9999px'
  ta.style.left = '-9999px'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  // 回退会抢走焦点;复制完还给原来的元素(比如对话框里的输入框)
  const prevFocus = document.activeElement as HTMLElement | null
  try {
    ta.focus()
    ta.select()
    ta.setSelectionRange(0, text.length)
    return typeof document.execCommand === 'function' && document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(ta)
    prevFocus?.focus?.()
  }
}

/** 没有自带「已复制」状态的调用点(右键菜单等)用:成功可选提示,失败一律 toast。 */
export async function copyTextOrToast(text: string, successMessage?: string): Promise<boolean> {
  const ok = await copyText(text)
  if (!ok) useToastStore.getState().add('复制失败,请手动选择', 'error')
  else if (successMessage) useToastStore.getState().add(successMessage, 'info')
  return ok
}
