import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ServicesList from './ServicesList'
import type { ServiceRow } from '../api/services'
import { useToastStore } from '../stores/toast'

// Hoisted mock state — vitest moves vi.mock calls to the top, so the
// module factories below close over these via the getter pattern.
const navigateSpy = vi.fn()
const useServicesMock = vi.fn()
const apiFetchMock = vi.fn()
const confirmMock = vi.fn()
const setAutostartMutate = vi.fn()

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

vi.mock('../api/services', async () => {
  const actual = await vi.importActual<typeof import('../api/services')>('../api/services')
  return {
    ...actual,
    useServices: () => useServicesMock(),
    // ServicesList 现在用 useDeleteService — mock 掉避免 QueryClientProvider
    useDeleteService: () => ({ mutate: vi.fn(), isPending: false }),
    // 开机启动 toggle 同理(fetchAutostartPreview 保持真身,走被 mock 的 apiFetch)。
    useSetServiceAutostart: () => ({ mutate: setAutostartMutate, isPending: false }),
  }
})

vi.mock('../stores/confirm', () => ({
  confirmDialog: (...args: unknown[]) => confirmMock(...args),
}))

// ServicesList 的 onImported 处理器自己调 apiFetch(见 api/client) 去按 service_name
// 查一次 /api/v1/services 拿新建服务的 id ——mock 掉方便控制「找到/找不到」两条分支。
vi.mock('../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

vi.mock('../components/services/CreateServiceDialog', () => ({
  default: () => null,
}))

// ImportComfyDialog 本身在专门的 ImportComfyDialog.test.tsx 里覆盖过了(文件解析、
// 校验、提交)。这里只需要驱动它的 onImported 回调,验证 ServicesList 自己那段
// 「查表拿 id → 导航 / 找不到就 toast 兜底」逻辑,所以整体替身成一个按钮。
vi.mock('../components/services/ImportComfyDialog', () => ({
  default: ({ onImported }: { onImported: (serviceName: string) => void }) => (
    <button type="button" onClick={() => onImported('minimax-h3-r2v')}>
      trigger-import
    </button>
  ),
}))

// ModelBadge pulls live engine/component status (useQuery + ws) — stub it so
// these navigation tests don't need a QueryClientProvider.
vi.mock('../api/serviceModels', () => ({
  useServiceModelStatus: () => ({ refs: [], total: 0, loaded: 0, loading: 0, failed: 0 }),
  MODEL_STATE_VIS: {
    loaded: { label: '已加载', color: '' },
    loading: { label: '加载中', color: '' },
    failed: { label: '失败', color: '' },
    cold: { label: '未加载', color: '' },
  },
}))

function makeService(over: Partial<ServiceRow> = {}): ServiceRow {
  return {
    id: '1234567890',
    name: 'sample-svc',
    type: 'inference',
    status: 'active',
    source_type: 'workflow',
    source_id: '55',
    source_name: null,
    category: 'llm',
    meter_dim: 'tokens',
    autostart: false,
    workflow_id: '55',
    workflow_name: 'demo-wf',
    snapshot_hash: 'sha256:abc',
    snapshot_schema_version: 1,
    version: 1,
    models: [],
    created_at: '2026-04-23T00:00:00Z',
    updated_at: '2026-04-23T00:00:00Z',
    ...over,
  }
}

beforeEach(() => {
  navigateSpy.mockReset()
  useServicesMock.mockReset()
  apiFetchMock.mockReset()
  confirmMock.mockReset()
  confirmMock.mockResolvedValue(true)
  setAutostartMutate.mockReset()
  useToastStore.setState({ toasts: [] })
})

describe('ServicesList 开机启动 (autostart)', () => {
  const PREVIEW = {
    service_id: '1234567890',
    name: 'sample-svc',
    autostart: false,
    preload_models: [{ name: 'qwen3_6_35b_a3b_fp8', vram_gb: 35, gpu: 1 }],
  }

  function renderWith(svc: Partial<ServiceRow> = {}) {
    useServicesMock.mockReturnValue({
      data: [makeService(svc)],
      isLoading: false,
      error: null,
    })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
  }

  it('右键菜单「设为开机启动」→ 先拉 preview,确认框列出开机会加载的模型', async () => {
    apiFetchMock.mockResolvedValue(PREVIEW)
    renderWith()

    fireEvent.contextMenu(screen.getByText('sample-svc'))
    fireEvent.click(screen.getByText('设为开机启动'))

    await waitFor(() => expect(confirmMock).toHaveBeenCalled())
    // preview 必须是先问后端拿的,不是前端瞎编的。
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/v1/services/1234567890/autostart-preview',
    )
    const msg = (confirmMock.mock.calls[0][0] as { message: string }).message
    expect(msg).toContain('开机将自动加载以下模型')
    expect(msg).toContain('qwen3_6_35b_a3b_fp8')
    expect(msg).toContain('35 GB')
    expect(msg).toContain('GPU 1')
  })

  it('确认后才发 POST(带 enabled: true)', async () => {
    apiFetchMock.mockResolvedValue(PREVIEW)
    renderWith()

    fireEvent.contextMenu(screen.getByText('sample-svc'))
    fireEvent.click(screen.getByText('设为开机启动'))

    await waitFor(() => expect(setAutostartMutate).toHaveBeenCalled())
    expect(setAutostartMutate.mock.calls[0][0]).toEqual({
      serviceId: '1234567890',
      enabled: true,
    })
  })

  it('确认框取消 → 一个 POST 都不发', async () => {
    apiFetchMock.mockResolvedValue(PREVIEW)
    confirmMock.mockResolvedValue(false)
    renderWith()

    fireEvent.contextMenu(screen.getByText('sample-svc'))
    fireEvent.click(screen.getByText('设为开机启动'))

    await waitFor(() => expect(confirmMock).toHaveBeenCalled())
    expect(setAutostartMutate).not.toHaveBeenCalled()
  })

  it('已开启的服务:卡片显示徽标,菜单项变成「取消开机启动」且不拉 preview', async () => {
    renderWith({ autostart: true })

    expect(screen.getByText('开机启动')).toBeTruthy()
    fireEvent.contextMenu(screen.getByText('sample-svc'))
    fireEvent.click(screen.getByText('取消开机启动'))

    await waitFor(() => expect(setAutostartMutate).toHaveBeenCalled())
    expect(setAutostartMutate.mock.calls[0][0]).toEqual({
      serviceId: '1234567890',
      enabled: false,
    })
    // 关掉不需要问「会加载什么」。
    expect(apiFetchMock).not.toHaveBeenCalled()
  })
})

describe('ServicesList card click → navigate', () => {
  it('uses react-router useNavigate (regression: previously used window.history.pushState which never re-rendered the detail overlay)', () => {
    useServicesMock.mockReturnValue({
      data: [makeService({ id: '999', name: 'click-me' })],
      isLoading: false,
      error: null,
    })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    // The card body and the explicit "详情" footer button are both wired
    // to the same navigate; click the body (named-region heuristic for
    // the click-me service).
    fireEvent.click(screen.getByText('click-me'))
    expect(navigateSpy).toHaveBeenCalledWith('/services/999')
  })

  it('falls back to onOpen prop when provided (no router involvement)', () => {
    useServicesMock.mockReturnValue({
      data: [makeService({ id: '777', name: 'override-me' })],
      isLoading: false,
      error: null,
    })
    const onOpen = vi.fn()
    render(
      <MemoryRouter>
        <ServicesList onOpen={onOpen} />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByText('override-me'))
    expect(onOpen).toHaveBeenCalledWith('777')
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('"新建服务" split button opens a menu with both creation paths', () => {
    useServicesMock.mockReturnValue({ data: [], isLoading: false, error: null })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /新建服务/ }))
    expect(screen.getByRole('menuitem', { name: /快速开通/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /导入 ComfyUI 工作流/ })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /从 Workflow 发布/ })).not.toBeInTheDocument()
  })

  it('renders the empty state CTA when there are no services', () => {
    useServicesMock.mockReturnValue({ data: [], isLoading: false, error: null })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    expect(screen.getByText(/还没有服务/)).toBeInTheDocument()
  })

  it('category=image 的服务有专属 IMAGE tab 可筛(真机:img-flux2 category=image 只在「全部」出现、子 tab 计数和与全部对不上)', () => {
    useServicesMock.mockReturnValue({
      data: [
        makeService({ id: '1', name: 'img-flux2', type: 'inference', category: 'image' }),
        makeService({ id: '2', name: 'qwen3-5-api', type: 'llm', category: null }),
      ],
      isLoading: false,
      error: null,
    })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    // 图像 tab 计数为 1（之前根本没有这个 tab → img-flux2 落进 image 桶但无处可筛）
    const imageTab = screen.getByRole('button', { name: /图像\s*1/ })
    expect(imageTab).toBeInTheDocument()
    // 点图像 tab 只剩 img-flux2
    fireEvent.click(imageTab)
    expect(screen.getByText('img-flux2')).toBeInTheDocument()
    expect(screen.queryByText('qwen3-5-api')).not.toBeInTheDocument()
  })

  it('category=null 的 llm 服务按 type 归入 LLM 分类(真机:qwen3-5-api type=llm/category=null 被误归「其他」)', () => {
    useServicesMock.mockReturnValue({
      data: [
        makeService({ id: '1', name: 'qwen3-5-api', type: 'llm', category: null }),
        makeService({ id: '2', name: 'ltx-drama', type: 'inference', category: 'app' }),
      ],
      isLoading: false,
      error: null,
    })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    // tab 计数:LLM 应为 1(不是 0),其他为 1
    const llmTab = screen.getByRole('button', { name: /LLM\s*1/ })
    expect(llmTab).toBeInTheDocument()
    // 点 LLM tab 只剩 qwen3-5-api
    fireEvent.click(llmTab)
    expect(screen.getByText('qwen3-5-api')).toBeInTheDocument()
    expect(screen.queryByText('ltx-drama')).not.toBeInTheDocument()
  })

  it('source_type=comfy_template 的服务显示「桥」徽标,且可用「ComfyUI 桥」tab 单独筛出', () => {
    useServicesMock.mockReturnValue({
      data: [
        makeService({ id: '1', name: 'minimax-h3-r2v', source_type: 'comfy_template', category: 'image' }),
        makeService({ id: '2', name: 'qwen3-5-api', source_type: 'workflow', category: 'llm' }),
      ],
      isLoading: false,
      error: null,
    })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    expect(screen.getByText('桥')).toBeInTheDocument()
    const bridgeTab = screen.getByRole('button', { name: /ComfyUI 桥\s*1/ })
    fireEvent.click(bridgeTab)
    expect(screen.getByText('minimax-h3-r2v')).toBeInTheDocument()
    expect(screen.queryByText('qwen3-5-api')).not.toBeInTheDocument()
  })

  it('「新建服务」菜单里有「导入 ComfyUI 工作流」入口', () => {
    useServicesMock.mockReturnValue({ data: [], isLoading: false, error: null })
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /新建服务/ }))
    expect(screen.getByRole('menuitem', { name: /导入 ComfyUI 工作流/ })).toBeInTheDocument()
  })

  it('导入成功后按 service_name 查 /api/v1/services 找到对应行,navigate 到它的 id', async () => {
    useServicesMock.mockReturnValue({ data: [], isLoading: false, error: null })
    apiFetchMock.mockResolvedValue([
      makeService({ id: '42', name: 'minimax-h3-r2v', source_type: 'comfy_template' }),
    ])
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByText('trigger-import'))
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/services/42'))
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/services')
  })

  it('service_name 在刷新出的列表里找不到匹配行时,toast 兜底而不 navigate', async () => {
    useServicesMock.mockReturnValue({ data: [], isLoading: false, error: null })
    apiFetchMock.mockResolvedValue([
      makeService({ id: '9', name: 'someone-else' }), // 没有 minimax-h3-r2v 这行
    ])
    render(
      <MemoryRouter>
        <ServicesList />
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByText('trigger-import'))
    await waitFor(() =>
      expect(useToastStore.getState().toasts.some((t) => t.message.includes('minimax-h3-r2v'))).toBe(
        true,
      ),
    )
    expect(navigateSpy).not.toHaveBeenCalled()
  })
})
