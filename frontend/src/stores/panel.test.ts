import { describe, it, expect, afterEach, vi } from 'vitest'
import { overlayForPath } from './panel'

describe('overlayForPath', () => {
  it('把列表页路由映射到对应 overlay', () => {
    expect(overlayForPath('/services')).toBe('services')
    expect(overlayForPath('/settings')).toBe('settings')
    expect(overlayForPath('/api-keys')).toBe('api-keys-list')
    expect(overlayForPath('/workflows')).toBe('workflows-list')
    expect(overlayForPath('/node-packages')).toBe('node-packages')
    expect(overlayForPath('/status')).toBe('status')
  })

  it('识别 React Router 接受的尾斜杠和大小写变体', () => {
    expect(overlayForPath('/dashboard/')).toBe('dashboard')
    expect(overlayForPath('/Dashboard')).toBe('dashboard')
    expect(overlayForPath('/services/')).toBe('services')
    expect(overlayForPath('/workflows/')).toBe('workflows-list')
  })

  it('详情页走各自独立的 overlay slot', () => {
    expect(overlayForPath('/services/abc')).toBe('service-detail')
    expect(overlayForPath('/api-keys/abc')).toBe('api-key-detail')
  })

  it('画布路由没有 overlay', () => {
    expect(overlayForPath('/workflows/42')).toBeNull()
    expect(overlayForPath('/')).toBeNull()
    expect(overlayForPath('/unknown')).toBeNull()
  })
})

describe('usePanelStore 初值', () => {
  afterEach(() => {
    vi.resetModules()
    window.history.pushState({}, '', '/')
  })

  // 硬刷新 /services 时,store 建出来的第一刻 activeOverlay 就得是 'services'。
  // 靠 RouteSync 的 effect 回填的话,NodeEditor 会先按画布模式 render 一轮。
  it('从 window.location 同步推出 activeOverlay,不等 RouteSync', async () => {
    window.history.pushState({}, '', '/services')
    vi.resetModules()
    const { usePanelStore } = await import('./panel')
    expect(usePanelStore.getState().activeOverlay).toBe('services')
  })

  it('画布路由下初值仍是 null', async () => {
    window.history.pushState({}, '', '/workflows/7')
    vi.resetModules()
    const { usePanelStore } = await import('./panel')
    expect(usePanelStore.getState().activeOverlay).toBeNull()
  })
})
