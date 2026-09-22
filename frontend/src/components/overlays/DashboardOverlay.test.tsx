import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import DashboardOverlay from './DashboardOverlay'
import type { GpuGroup } from '../../api/engines'
import type { SysGpuInfo } from '../../api/system'

// DashboardOverlay 拉一堆 api —— 全部 mock 成空/最小数据，只验 GPU 卡片区。
// GPU / 组两份数据放在 hoisted 盒子里，每个用例开头改写（vi.mock 工厂会被提升到
// import 之前，不能直接闭包捕获 describe 内的变量）。
const box = vi.hoisted(() => ({
  gpus: [] as SysGpuInfo[],
  groups: [] as GpuGroup[],
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
  useVLLMMetrics: () => ({ data: { instances: [] }, isLoading: false, error: null }),
}))
vi.mock('../../api/engines', () => ({
  useEngines: () => ({ data: [] }),
  useLoadedAdapters: () => ({ data: { count: 0, entries: [] } }),
  useUnloadImageAdapters: () => ({ mutate: () => {}, isPending: false }),
  useGpuGroups: () => ({ data: { groups: box.groups } }),
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

/** 系统状态默认收起 —— 每个用例先点开。 */
function renderSystemPanel() {
  render(<DashboardOverlay />)
  fireEvent.click(screen.getByText('系统状态'))
}

beforeEach(() => {
  box.gpus = THREE_GPUS
  box.groups = []
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
    expect(screen.getByText('3× GPU · 0 模型常驻')).toBeTruthy()
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
    expect(screen.getByText('3× GPU（1 组） · 0 模型常驻')).toBeTruthy()
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
