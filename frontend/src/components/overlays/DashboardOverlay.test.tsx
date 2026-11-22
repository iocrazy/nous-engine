import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import DashboardOverlay from './DashboardOverlay'
import type { GpuGroup } from '../../api/engines'
import type { VLLMInstance } from '../../api/vllm'
import type { SysGpuInfo } from '../../api/system'

// DashboardOverlay 拉一堆 api —— 全部 mock 成空/最小数据，只验 GPU 卡片区。
// GPU / 组两份数据放在 hoisted 盒子里，每个用例开头改写（vi.mock 工厂会被提升到
// import 之前，不能直接闭包捕获 describe 内的变量）。
const box = vi.hoisted(() => ({
  gpus: [] as SysGpuInfo[],
  comfyQueue: 0,
  comfyOnline: true,
  clearCache: vi.fn(),
  restartComfy: vi.fn(),
  restartPending: false,
  restartResult: undefined as boolean | undefined,
  confirmDialog: vi.fn(),
  vllm: [] as VLLMInstance[],
  groups: [] as GpuGroup[],
  engines: [] as Partial<import('../../api/engines').EngineInfo>[],
}))

const mkGpu = (over: Partial<SysGpuInfo> & { index: number }): SysGpuInfo => ({
  name: 'NVIDIA GeForce RTX 3090',
  utilization_gpu: 0, utilization_memory: 0,
  temperature: 31, fan_speed: 0, power_draw_w: 40, power_limit_w: 370,
  memory_used_mb: 0, memory_total_mb: 24576, memory_free_mb: 24576,
  processes: [],
  ...over,
})

const THREE_GPUS: SysGpuInfo[] = [
  mkGpu({
    index: 0, utilization_gpu: 12, memory_used_mb: 20480, memory_free_mb: 4096,
    processes: [
      {
        pid: 100, gpu: 0, used_gpu_memory_mb: 19968, name: 'python',
        command: 'vllm serve', managed: true, model_name: 'qwen3_6_35b_a3b_fp8',
      },
      {
        pid: 300, gpu: 0, used_gpu_memory_mb: 512, name: 'Xorg',
        command: '/usr/lib/Xorg', managed: false, model_name: null,
      },
    ],
  }),
  mkGpu({
    index: 1, name: 'NVIDIA RTX PRO 6000', utilization_gpu: 40,
    memory_used_mb: 20000, memory_total_mb: 98304, memory_free_mb: 78304,
    temperature: 55, fan_speed: 30, power_draw_w: 200, power_limit_w: 600,
  }),
  mkGpu({
    index: 2, utilization_gpu: 11, memory_used_mb: 19968, memory_free_mb: 4608,
    processes: [
      {
        pid: 101, gpu: 2, used_gpu_memory_mb: 19968, name: 'python',
        command: 'vllm serve', managed: true, model_name: 'qwen3_6_35b_a3b_fp8',
      },
    ],
  }),
]

const LLM_TP: GpuGroup = {
  id: 'llm-tp', gpus: [0, 2], name: 'NVIDIA GeForce RTX 3090',
  nvlink: true, total_gb: 47.2,
}

vi.mock('../../api/dashboard', () => ({ useDashboardSummary: () => ({ data: undefined }) }))
vi.mock('../../api/observability', () => ({ useRuntimeMetrics: () => ({ data: undefined, isLoading: false, error: null }) }))
vi.mock('../../api/vllm', () => ({
  useVLLMMetrics: () => ({ data: { instances: box.vllm }, isLoading: false, error: null }),
}))
vi.mock('../../api/engines', () => ({
  useEngines: () => ({ data: box.engines }),
  useLoadedAdapters: () => ({ data: { count: 0, entries: [] } }),
  useGpuGroups: () => ({ data: { groups: box.groups } }),
}))
vi.mock('../../api/services', () => ({ useServices: () => ({ data: [] }) }))
vi.mock('../../api/health', () => ({
  useHealth: () => ({ data: { comfy: { listens: ['127.0.0.1'], missing_listens: [] } } }),
}))
vi.mock('../../api/status', () => ({
  useStatus: () => ({ data: { overall: 'degraded', components: [{ key: 'comfy', name: 'ComfyUI 桥', status: 'degraded' }] } }),
  useStatusConnections: () => ({
    data: {
      tailscale: [
        { key: 'backend', url: 'http://100.64.0.42:8000', http_status: 200 },
        { key: 'comfy', url: 'http://100.64.0.42:8888', http_status: null },
      ],
      comfy_unit: { started_at: 1794978664, restart_count: 3 },
      checked_at: '2026-11-18T05:30:00Z',
    },
  }),
}))
vi.mock('../../stores/confirm', () => ({ confirmDialog: box.confirmDialog }))
vi.mock('../../api/comfyTemplates', () => ({
  useComfyMaintenance: () => ({ mutate: box.clearCache, reset: vi.fn() }),
  useRestartComfy: () => ({ mutate: box.restartComfy, reset: vi.fn(),
    isPending: box.restartPending, isSuccess: box.restartResult !== undefined, data: box.restartResult }),
  useComfyHealth: () => ({
    data: { online: box.comfyOnline, queue_depth: box.comfyQueue, process_rss_bytes: 4 * 1024 ** 3,
      base_url: 'http://127.0.0.1:8888', running_render: null },
  }),
}))
vi.mock('../../api/system', () => ({
  useSysGpus: () => ({ data: { count: box.gpus.length, gpus: box.gpus } }),
  useSysStats: () => ({ data: undefined }),
  useSysProcesses: () => ({ data: undefined }),
  useKillProcess: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../api/runners', () => ({
  useRunners: () => ({
    data: [
      { id: 'runner-i', label: 'Runner-I', role: 'image', state: 'busy',
        current_task: null, queue: [], restart_attempt: null, load_error: null, gpus: [1] },
    ],
  }),
}))

function renderSystemPanel() {
  render(<DashboardOverlay />)
  fireEvent.click(screen.getByRole('tab', { name: '系统状态' }))
}

function renderComfyPanel() {
  render(<DashboardOverlay />)
  fireEvent.click(screen.getByRole('tab', { name: 'ComfyUI' }))
}

beforeEach(() => {
  box.gpus = THREE_GPUS
  box.comfyQueue = 0
  box.comfyOnline = true
  box.clearCache.mockReset()
  box.restartComfy.mockReset()
  box.restartPending = false
  box.restartResult = undefined
  box.confirmDialog.mockReset()
  box.vllm = []
  box.groups = []
  box.engines = []
})

describe('DashboardOverlay tabs', () => {
  it('places ComfyUI restart in its own tab and requires confirmation', async () => {
    box.confirmDialog.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    render(<DashboardOverlay />)
    expect(screen.queryByRole('button', { name: '重启 ComfyUI' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看系统状态' }))
    expect(screen.queryByRole('button', { name: '重启 ComfyUI' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'ComfyUI' }))
    const restart = screen.getByRole('button', { name: '重启 ComfyUI' })
    fireEvent.click(restart)
    await waitFor(() => expect(restart.hasAttribute('disabled')).toBe(false))
    expect(box.restartComfy).not.toHaveBeenCalled()
    fireEvent.click(restart)
    await waitFor(() => expect(box.restartComfy).toHaveBeenCalledTimes(1))
  })

  it('shows why restart is unavailable while there are queued tasks', () => {
    box.comfyQueue = 1
    renderComfyPanel()
    expect(screen.getByRole('button', { name: '重启 ComfyUI' })).toBeDisabled()
    expect(screen.getByText('任务结束后可执行维护。')).toBeVisible()
  })

  it('reports each physical GPU free memory separately', () => {
    box.gpus = [
      mkGpu({ index: 0, memory_free_mb: 1024 }),
      mkGpu({ index: 1, memory_free_mb: 3072 }),
    ]
    render(<DashboardOverlay />)
    expect(screen.getByRole('button', { name: /GPU 0 剩余显存 1\.0 GiB/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /GPU 1 剩余显存 3\.0 GiB/ })).toBeTruthy()
    expect(screen.queryByText('4.0 GiB')).toBeNull()
  })

  it('locks other maintenance actions while cache clearing is being confirmed', async () => {
    let confirm!: (value: boolean) => void
    box.confirmDialog.mockReturnValue(new Promise<boolean>((resolve) => { confirm = resolve }))
    renderComfyPanel()
    fireEvent.click(screen.getByRole('button', { name: '清理缓存' }))
    expect(screen.getByRole('button', { name: '重启 ComfyUI' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '释放显存' })).toBeDisabled()
    confirm(true)
    await waitFor(() => expect(box.clearCache).toHaveBeenCalledWith('clear'))
  })

  it.each([
    { pending: true, result: undefined, message: '正在重启 ComfyUI，等待服务恢复…' },
    { pending: false, result: false, message: '重启请求已接收，暂未确认服务恢复。' },
    { pending: false, result: true, message: 'ComfyUI 重启成功，服务已重新上线。' },
  ])('reports restart verification ($message)', ({ pending, result, message }) => {
    box.restartPending = pending
    box.restartResult = result
    renderComfyPanel()
    expect(screen.getByRole('status')).toHaveTextContent(message)
  })

  it('requires confirmation before clearing the idle ComfyUI cache', async () => {
    box.confirmDialog.mockResolvedValue(true)
    renderComfyPanel()
    expect(screen.getByText('4.0 GiB')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '清理缓存' }))
    await waitFor(() => expect(box.clearCache).toHaveBeenCalledWith('clear'))
    expect(box.confirmDialog).toHaveBeenCalledOnce()
  })

  it('submits a separate operation when releasing VRAM', async () => {
    box.confirmDialog.mockResolvedValue(true)
    renderComfyPanel()
    fireEvent.click(screen.getByRole('button', { name: '释放显存' }))
    await waitFor(() => expect(box.clearCache).toHaveBeenCalledWith('release'))
    expect(box.clearCache).toHaveBeenCalledOnce()
  })

  it.each([{ queue: 1, online: true }, { queue: 0, online: false }])(
    'blocks clearing when ComfyUI cannot confirm an idle queue (%o)',
    ({ queue, online }) => {
      box.comfyQueue = queue
      box.comfyOnline = online
      renderComfyPanel()
      const button = screen.getByRole<HTMLButtonElement>('button', { name: '清理缓存' })
      expect(button.disabled).toBe(true)
      fireEvent.click(button)
      expect(box.confirmDialog).not.toHaveBeenCalled()
    },
  )

  it('keeps vLLM capacity, scheduling and prefix metrics explicit', () => {
    box.vllm = [
      {
        name: 'chat', port: 47381, healthy: true, error: null,
        config: { num_gpu_blocks: '191', block_size: '800', enable_prefix_caching: 'True' },
        stats: { kv_cache_usage_perc: 0.34, running: 2, waiting: 0, prefix_cache_hit_rate: 0.64,
          prefix_cache_hits_total: 64, prefix_cache_queries_total: 100 },
      },
      {
        name: 'embed', port: 46929, healthy: true, error: null,
        config: { enable_prefix_caching: 'False' }, stats: {},
      },
      {
        name: 'rerank', port: 41459, healthy: true, error: null,
        config: { enable_prefix_caching: 'True' },
        stats: { prefix_cache_queries_total: 0, prefix_cache_hits_total: 0, prefix_cache_hit_rate: 0 },
      },
    ]
    renderSystemPanel()
    const table = within(screen.getByRole('table', { name: 'vLLM 实例指标' }))
    const chat = within(table.getByRole('row', { name: /chat/ }))
    expect(chat.getByText('端口 47381')).toBeTruthy()
    expect(chat.getByText('34%')).toBeTruthy()
    expect(chat.getByTitle('152,800 tokens')).toBeTruthy()
    expect(chat.getByText('191 blocks × 800')).toBeTruthy()
    expect(chat.getByText('2')).toBeTruthy()
    expect(chat.getByText('0')).toBeTruthy()
    expect(chat.getByText('64%')).toBeTruthy()
    expect(chat.getByText('64 / 100')).toBeTruthy()
    expect(within(table.getByRole('row', { name: /embed/ })).getByText('关闭')).toBeTruthy()
    expect(within(table.getByRole('row', { name: /rerank/ })).getByText('暂无查询')).toBeTruthy()
  })

  it('places service and connection health immediately after resources', () => {
    renderSystemPanel()
    const resources = screen.getByRole('region', { name: '资源与运行时' })
    const connections = screen.getByRole('region', { name: '服务与连接' })
    expect(resources.nextElementSibling).toBe(connections)
    expect(screen.getByText('HTTP 200')).toBeTruthy()
    expect(screen.queryByText('http://127.0.0.1:8888')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '清理缓存' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: 'ComfyUI' }))
    expect(screen.getByText('队列任务')).toBeVisible()
    expect(screen.getByText('http://127.0.0.1:8888')).toBeVisible()
    expect(screen.getByText('监听地址（1）').closest('details')?.open).toBe(false)
    expect(screen.getByText(/探测失败/)).toBeVisible()
    expect(screen.getByText('自动重启次数')).toBeVisible()
    expect(screen.getByText('最近启动').nextElementSibling?.textContent).not.toBe('—')
  })

  it('opens the model tab filtered to failed models from the overview', () => {
    box.engines = [
      { name: 'active-engine', status: 'loaded', type: 'llm' },
      { name: 'failed-engine', status: 'failed', type: 'llm', status_detail: 'GPU memory exhausted' },
    ]
    render(<DashboardOverlay />)
    fireEvent.click(screen.getByRole('button', { name: /加载失败.*查看模型/ }))
    expect(screen.getByRole('tab', { name: '模型' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText('failed-engine')).toBeTruthy()
    expect(screen.queryByText('active-engine')).toBeNull()
  })
})

describe('DashboardOverlay GPU 卡片区 —— 无组（零回归）', () => {
  it('三张物理卡各画一张，没有合并卡/展开按钮', () => {
    renderSystemPanel()
    expect(screen.getByText('GPU 0')).toBeTruthy()
    expect(screen.getByText('GPU 1')).toBeTruthy()
    expect(screen.getByText('GPU 2')).toBeTruthy()
    expect(screen.queryByText('展开物理卡')).toBeNull()
    expect(screen.queryByText(/llm-tp/)).toBeNull()
    // 顶部统计不带组信息
    expect(screen.getByText('3× GPU')).toBeTruthy()
    // 单卡进程行照旧逐条：两张卡各自一条（tp=2 的两个 pid），不做合并
    expect(screen.getAllByText('qwen3_6_35b_a3b_fp8')).toHaveLength(2)
    expect(screen.queryByText(/^tp=/)).toBeNull()
  })

  it('GPU 卡片仍显示归属的 runner 标签（DD3）', () => {
    renderSystemPanel()
    expect(screen.getByText(/Runner-I \(image\)/)).toBeTruthy()
  })

  it('组接口返回空数组时与 undefined 行为一致', () => {
    box.groups = []
    renderSystemPanel()
    expect(screen.getAllByText(/^GPU [0-9]$/)).toHaveLength(3)
  })
})

describe('DashboardOverlay GPU 卡片区 —— 有组 [0,2]', () => {
  beforeEach(() => {
    box.groups = [LLM_TP]
  })

  it('把组内两张卡合成一张大卡，GPU 1 仍单独一张', () => {
    renderSystemPanel()
    expect(screen.getByText('GPU 0+2 · llm-tp')).toBeTruthy()
    expect(screen.getByText('NVIDIA GeForce RTX 3090 ×2')).toBeTruthy()
    expect(screen.getByText('NVLink')).toBeTruthy()
    expect(screen.getByText('GPU 1')).toBeTruthy()
    // 折叠时组内物理卡不单独出现
    expect(screen.queryByText('GPU 0')).toBeNull()
    expect(screen.queryByText('GPU 2')).toBeNull()
    // 顶部统计反映组
    expect(screen.getByText('3× GPU（1 组）')).toBeTruthy()
  })

  it('MEM 条是组内合计，利用率取最大并列出每卡', () => {
    renderSystemPanel()
    // (20480 + 19968) / 1024 = 39.5G，(24576 * 2) / 1024 = 48.0G
    expect(screen.getByText('39.5G / 48.0G')).toBeTruthy()
    // max(12, 11) = 12%
    expect(screen.getByText('#0 12% · #2 11%')).toBeTruthy()
    // 温度/风扇/功耗逐卡一行
    expect(screen.getAllByText('#0')).not.toHaveLength(0)
  })

  it('同名 managed 进程合成一行，显存合计 + 每卡分解 + tp 徽标', () => {
    renderSystemPanel()
    expect(screen.getAllByText('qwen3_6_35b_a3b_fp8')).toHaveLength(1)
    expect(screen.getByText('tp=2')).toBeTruthy()
    // 合计 39936MB = 39.0G，分解 #0 19.5G · #2 19.5G
    const cell = screen.getByTitle('#0 PID 100 #2 PID 101')  // getByTitle 会归一化换行
    expect(cell.textContent).toBe('39.0G(#0 19.5G · #2 19.5G)')
  })

  it('orphan 进程逐条列出并标明所在卡，保留 kill 按钮', () => {
    renderSystemPanel()
    expect(screen.getByText('orphan')).toBeTruthy()
    expect(screen.getByText('/usr/lib/Xorg')).toBeTruthy()
    // orphan 行头部的卡号标记
    const tags = screen.getAllByText('#0')
    expect(tags.length).toBeGreaterThan(1) // 温度行 + orphan 行
    expect(screen.getByTitle('Kill 300')).toBeTruthy()
  })

  it('点「展开物理卡」后在合并卡下方出现两张物理卡', () => {
    renderSystemPanel()
    fireEvent.click(screen.getByText('展开物理卡'))
    expect(screen.getByText('GPU 0')).toBeTruthy()
    expect(screen.getByText('GPU 2')).toBeTruthy()
    // 合并卡还在
    expect(screen.getByText('GPU 0+2 · llm-tp')).toBeTruthy()
    // 再点收起
    fireEvent.click(screen.getByText('收起物理卡'))
    expect(screen.queryByText('GPU 0')).toBeNull()
  })

  it('组内有卡缺指标时整组降级成单卡，不画缺卡的合并卡', () => {
    box.gpus = THREE_GPUS.filter((g) => g.index !== 2)
    renderSystemPanel()
    expect(screen.queryByText(/llm-tp/)).toBeNull()
    expect(screen.getByText('GPU 0')).toBeTruthy()
    expect(screen.getByText('GPU 1')).toBeTruthy()
  })
})
