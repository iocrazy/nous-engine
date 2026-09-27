import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SettingsOverlay from './SettingsOverlay'

// m16 mockup 对齐：3 分组 sub-nav（通用/推理/数据/高级 — 共 10 项）。
// 默认进 账号；切到节点包 / 关于 / 引擎默认 都能渲染。

vi.mock('../../api/settings', () => ({
  useServerSettings: () => ({ data: undefined }),
  useUpdateServerSettings: () => ({ mutate: vi.fn(), isPending: false }),
}))

vi.mock('../../api/nodes', () => ({
  useNodePackages: () => ({ data: {}, isLoading: false }),
  useRescanPackages: () => ({ mutate: vi.fn(), isPending: false }),
  useInstallPackageZip: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useInstallPackageGit: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUninstallPackage: () => ({ mutateAsync: vi.fn(), isPending: false, variables: undefined }),
  useInstallPackageDeps: () => ({ mutateAsync: vi.fn(), isPending: false, variables: undefined }),
  useTogglePackage: () => ({ mutateAsync: vi.fn(), isPending: false, variables: undefined }),
}))

vi.mock('../../api/comfyTemplates', () => ({
  getComfyHealth: vi.fn().mockResolvedValue({
    online: true,
    queue_depth: 0,
    version: '0.4.12',
    base_url: 'http://localhost:8188',
    timeout_s: 120,
  }),
}))

function withQuery(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

describe('SettingsOverlay sub-nav (m16 alignment)', () => {
  it('default shows 账号 panel', () => {
    render(withQuery(<SettingsOverlay />))
    expect(screen.getByText('本实例绑定的操作员账号与登录凭据')).toBeTruthy()
  })

  it('renders all 4 nav groups', () => {
    render(withQuery(<SettingsOverlay />))
    // 分组标题：通用 / 推理 / 数据 / 高级
    expect(screen.getByText('通用')).toBeTruthy()
    expect(screen.getByText('推理')).toBeTruthy()
    expect(screen.getByText('数据')).toBeTruthy()
    expect(screen.getByText('高级')).toBeTruthy()
  })

  it('switches to 节点包 sub-page', () => {
    render(withQuery(<SettingsOverlay />))
    fireEvent.click(screen.getByText('Workflow 节点包'))
    expect(screen.getByText(/0 个已安装/)).toBeTruthy()
  })

  it('switches to 关于 sub-page', () => {
    render(withQuery(<SettingsOverlay />))
    fireEvent.click(screen.getByText('关于'))
    expect(screen.getByText('Nous Engine')).toBeTruthy()
    expect(screen.getByText('协议兼容')).toBeTruthy()
  })

  it('switches to 引擎默认 sub-page (server-bound KV form)', () => {
    render(withQuery(<SettingsOverlay />))
    fireEvent.click(screen.getByText('引擎默认'))
    expect(screen.getByText('本地模型目录')).toBeTruthy()
    expect(screen.getByText('IndexTTS 仓库')).toBeTruthy()
  })

  it('switches to ComfyUI 桥 sub-page', () => {
    render(withQuery(<SettingsOverlay />))
    fireEvent.click(screen.getByText('ComfyUI 桥'))
    expect(screen.getByText('ComfyUI 工作流编排 - 网络连接与队列状态')).toBeTruthy()
  })

  it('placeholder sub-pages render the "敬请期待" hint', () => {
    render(withQuery(<SettingsOverlay />))
    fireEvent.click(screen.getByText('限流与配额'))
    expect(screen.getByText('敬请期待')).toBeTruthy()
  })
})
