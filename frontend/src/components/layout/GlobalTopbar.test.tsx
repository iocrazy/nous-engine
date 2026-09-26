import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import GlobalTopbar from './GlobalTopbar'
import type { Health } from '../../api/health'

vi.mock('../../api/tasks', () => ({ useTasks: () => ({ data: [] }) }))
vi.mock('../../api/admin', () => ({
  useAdminLogout: () => ({ mutate: vi.fn(), isPending: false }),
  useAdminMe: () => ({ data: { login_required: false } }),
}))
vi.mock('../../api/gpuStats', () => ({
  useGpuStats: () => ({ data: [] }),
  useSystemStats: () => ({ data: undefined }),
  pickPrimaryGpu: () => null,
}))

const healthState: { data?: Health; isError: boolean } = { isError: false }
vi.mock('../../api/health', async (orig) => ({
  ...(await orig<typeof import('../../api/health')>()),
  useHealth: () => healthState,
}))

function renderStatus() {
  render(<MemoryRouter><GlobalTopbar /></MemoryRouter>)
  return screen.getByTestId('infra-status')
}

describe('GlobalTopbar infra status — reads /health', () => {
  beforeEach(() => {
    healthState.data = undefined
    healthState.isError = false
  })

  it('healthy when /health status is ok', () => {
    healthState.data = { status: 'ok', database: 'ok', load_failures: {} }
    const el = renderStatus()
    expect(el.textContent).toContain('healthy')
  })

  it('down when /health cannot be fetched', () => {
    healthState.isError = true
    const el = renderStatus()
    expect(el.textContent).toContain('down')
    expect(el.textContent).not.toContain('healthy')
  })

  it('degraded lists database / load_failures / comfy problems in the title', () => {
    healthState.data = {
      status: 'degraded',
      database: 'error',
      load_failures: { 'qwen3.8': 'OOM' },
      comfy: {
        state: 'degraded', online: true, identity: 'foreign',
        listens: ['127.0.0.1'], missing_listens: ['100.124.149.118'],
        problems: ['占用 8888 的不是 systemd 管理的实例(pid 4242)', '缺少监听地址 100.124.149.118'],
      },
    }
    const el = renderStatus()
    expect(el.textContent).toContain('degraded')
    const title = el.getAttribute('title') ?? ''
    expect(title).toContain('database error')
    expect(title).toContain('qwen3.8')
    expect(title).toContain('占用 8888 的不是 systemd 管理的实例(pid 4242)')
    expect(title).toContain('缺少监听地址 100.124.149.118')
  })
})
