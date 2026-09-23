import { describe, it, expect, vi, afterEach } from 'vitest'
import { copyText } from './clipboard'

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
const originalSecure = Object.getOwnPropertyDescriptor(window, 'isSecureContext')
const originalExec = Object.getOwnPropertyDescriptor(document, 'execCommand')

function setEnv(opts: {
  secure: boolean
  clipboard?: { writeText: (t: string) => Promise<void> }
  exec?: (cmd: string) => boolean
}) {
  Object.defineProperty(window, 'isSecureContext', { value: opts.secure, configurable: true })
  Object.defineProperty(navigator, 'clipboard', { value: opts.clipboard, configurable: true })
  Object.defineProperty(document, 'execCommand', { value: opts.exec, configurable: true })
}

function restore(target: object, key: string, desc: PropertyDescriptor | undefined) {
  if (desc) Object.defineProperty(target, key, desc)
  else delete (target as Record<string, unknown>)[key]
}

afterEach(() => {
  restore(navigator, 'clipboard', originalClipboard)
  restore(window, 'isSecureContext', originalSecure)
  restore(document, 'execCommand', originalExec)
  vi.restoreAllMocks()
})

describe('copyText', () => {
  it('安全上下文:走 navigator.clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    const exec = vi.fn().mockReturnValue(true)
    setEnv({ secure: true, clipboard: { writeText }, exec })

    expect(await copyText('hello')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('hello')
    expect(exec).not.toHaveBeenCalled()
  })

  it('非安全上下文(明文 HTTP 内网):无 navigator.clipboard → execCommand 回退且返回 true', async () => {
    let copied: string | null = null
    const exec = vi.fn((cmd: string) => {
      // 回退复制的是被选中的临时 textarea
      copied = (document.activeElement as HTMLTextAreaElement | null)?.value ?? null
      return cmd === 'copy'
    })
    setEnv({ secure: false, clipboard: undefined, exec })

    expect(await copyText('secret-123')).toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
    expect(copied).toBe('secret-123')
    // 临时 textarea 用完即删
    expect(document.querySelectorAll('textarea').length).toBe(0)
  })

  it('clipboard API 被拒时也会落到 execCommand', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    const exec = vi.fn().mockReturnValue(true)
    setEnv({ secure: true, clipboard: { writeText }, exec })

    expect(await copyText('x')).toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('两路都失败返回 false,不抛', async () => {
    setEnv({ secure: false, clipboard: undefined, exec: vi.fn().mockReturnValue(false) })
    expect(await copyText('x')).toBe(false)

    setEnv({
      secure: false,
      clipboard: undefined,
      exec: vi.fn(() => {
        throw new Error('boom')
      }),
    })
    expect(await copyText('x')).toBe(false)

    setEnv({ secure: false, clipboard: undefined, exec: undefined })
    expect(await copyText('x')).toBe(false)
    expect(document.querySelectorAll('textarea').length).toBe(0)
  })
})
