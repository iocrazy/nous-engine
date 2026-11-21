import { describe, it, expect } from 'vitest'
import { overlayForPath } from './panel'

describe('overlayForPath', () => {
  it('把列表页路由映射到对应 overlay', () => {
    expect(overlayForPath('/services')).toBe('services')
    expect(overlayForPath('/settings')).toBe('settings')
    expect(overlayForPath('/api-keys')).toBe('api-keys-list')
    expect(overlayForPath('/node-packages')).toBe('node-packages')
    expect(overlayForPath('/status')).toBe('status')
  })

  it('识别 React Router 接受的尾斜杠和大小写变体', () => {
    expect(overlayForPath('/dashboard/')).toBe('dashboard')
    expect(overlayForPath('/Dashboard')).toBe('dashboard')
    expect(overlayForPath('/services/')).toBe('services')
  })

  it('详情页走各自独立的 overlay slot', () => {
    expect(overlayForPath('/services/abc')).toBe('service-detail')
    expect(overlayForPath('/api-keys/abc')).toBe('api-key-detail')
  })

  it('未匹配的地址没有导航选中项', () => {
    expect(overlayForPath('/workflows/42')).toBeNull()
    expect(overlayForPath('/')).toBeNull()
    expect(overlayForPath('/unknown')).toBeNull()
  })
})
