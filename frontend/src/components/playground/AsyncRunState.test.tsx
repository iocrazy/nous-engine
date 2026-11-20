import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AsyncRunState from './AsyncRunState'
import * as api from '../../api/predictions'
import type { Prediction } from '../../api/predictions'

vi.mock('../../api/predictions', () => ({
  getPrediction: vi.fn(),
  cancelPrediction: vi.fn(),
  createPredictionAsync: vi.fn(),
}))

function pred(overrides: Partial<Prediction>): Prediction {
  return {
    id: 'p1',
    service: 'svc',
    status: 'starting',
    input: {},
    output: null,
    error: null,
    progress: { nodes_done: 0, nodes_total: 2 },
    metrics: {},
    created_at: new Date().toISOString(),
    started_at: null,
    completed_at: null,
    ...overrides,
  }
}

function callbacks() {
  return { onDone: vi.fn(), onError: vi.fn(), onCancel: vi.fn() }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('AsyncRunState', () => {
  it('updates studio elapsed time and the reported sampler steps every second', async () => {
    const created = new Date().toISOString()
    const progress = (step: number) => pred({
      created_at: created, status: 'processing', progress: { nodes_done: 0, nodes_total: 2,
        comfy: { phase: 'running', label: '采样器', step, total: 25 } },
    })
    vi.mocked(api.getPrediction)
      .mockResolvedValueOnce(pred({
        created_at: created, status: 'processing', progress: { nodes_done: 0, nodes_total: 2,
          comfy: { phase: 'running', label: '优化提示词' } },
      }))
      .mockResolvedValueOnce(progress(4)).mockResolvedValue(progress(5))
    render(<AsyncRunState predictionId="p1" compact {...callbacks()} />)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('status')).toHaveTextContent('优化提示词 · 0:00')
    expect(screen.getByRole('progressbar', { name: '当前节点进度' })).not.toHaveAttribute('value')
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByRole('status')).toHaveTextContent('采样器 · 0:01 · 4/25 步 · 16%')
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByRole('status')).toHaveTextContent('采样器 · 0:02 · 5/25 步 · 20%')
    expect(screen.getByRole('progressbar', { name: '当前节点进度' })).toHaveAttribute('value', '5')
  })

  it('轮询推进 已提交/排队中 → 运行中 → 完成,succeeded 时把 output 交给 onDone', async () => {
    const cb = callbacks()
    vi.mocked(api.getPrediction)
      .mockResolvedValueOnce(pred({ status: 'starting' }))
      .mockResolvedValueOnce(pred({ status: 'processing', progress: { nodes_done: 1, nodes_total: 2 } }))
      .mockResolvedValueOnce(pred({
        status: 'succeeded',
        output: { outputs: { '92': { video_url: '/x.mp4' } } },
      }))

    render(<AsyncRunState predictionId="p1" {...cb} />)

    await act(async () => { await Promise.resolve() })
    expect(screen.getByText('排队中')).toBeInTheDocument()
    expect(cb.onDone).not.toHaveBeenCalled()

    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(screen.getByText('运行中')).toBeInTheDocument()
    expect(cb.onDone).not.toHaveBeenCalled()

    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(cb.onDone).toHaveBeenCalledTimes(1)
    expect(cb.onDone).toHaveBeenCalledWith({ outputs: { '92': { video_url: '/x.mp4' } } })
    expect(cb.onError).not.toHaveBeenCalled()
    expect(cb.onCancel).not.toHaveBeenCalled()
  })

  it('取消按钮调用 cancelPrediction 再触发 onCancel', async () => {
    const cb = callbacks()
    vi.mocked(api.getPrediction).mockResolvedValue(pred({ status: 'processing' }))
    vi.mocked(api.cancelPrediction).mockResolvedValue(pred({ status: 'canceled' }))

    render(<AsyncRunState predictionId="p1" {...cb} />)
    await act(async () => { await Promise.resolve() })

    fireEvent.click(screen.getByRole('button', { name: /取消/ }))
    await act(async () => { await Promise.resolve() })

    expect(api.cancelPrediction).toHaveBeenCalledWith('p1')
    expect(cb.onCancel).toHaveBeenCalledTimes(1)
    expect(cb.onDone).not.toHaveBeenCalled()
    expect(cb.onError).not.toHaveBeenCalled()
  })

  it('取消请求与渲染完成赛跑赢下(cancel 返回 succeeded)时结算为 onDone,而非 onCancel', async () => {
    // 镜像后端 cancel_prediction 的 TOCTOU 守护:interrupt 窗口内渲染先跑完,取消端点
    // 老实返回 succeeded+output——前端不该把这个已经成功的结果当成「已取消」丢掉。
    const cb = callbacks()
    vi.mocked(api.getPrediction).mockResolvedValue(pred({ status: 'processing' }))
    vi.mocked(api.cancelPrediction).mockResolvedValue(
      pred({ status: 'succeeded', output: { outputs: { '92': { video_url: '/race.mp4' } } } }),
    )

    render(<AsyncRunState predictionId="p1" {...cb} />)
    await act(async () => { await Promise.resolve() })

    fireEvent.click(screen.getByRole('button', { name: /取消/ }))
    await act(async () => { await Promise.resolve() })

    expect(cb.onDone).toHaveBeenCalledWith({ outputs: { '92': { video_url: '/race.mp4' } } })
    expect(cb.onCancel).not.toHaveBeenCalled()
  })

  it('failed 终态展示后端 error 文案,并把父组件解锁交给 onError(而非 onDone/onCancel)', async () => {
    const cb = callbacks()
    vi.mocked(api.getPrediction).mockResolvedValue(pred({ status: 'failed', error: '节点 92 崩了' }))

    render(<AsyncRunState predictionId="p1" {...cb} />)
    await act(async () => { await Promise.resolve() })

    expect(screen.getByText('节点 92 崩了')).toBeInTheDocument()
    expect(cb.onError).toHaveBeenCalledTimes(1)
    expect(cb.onError).toHaveBeenCalledWith('节点 92 崩了')
    expect(cb.onDone).not.toHaveBeenCalled()
    expect(cb.onCancel).not.toHaveBeenCalled()
  })

  it('轮询探测到非按钮触发的 canceled(外部取消)也要解锁 —— 走 onCancel', async () => {
    const cb = callbacks()
    vi.mocked(api.getPrediction).mockResolvedValue(pred({ status: 'canceled' }))

    render(<AsyncRunState predictionId="p1" {...cb} />)
    await act(async () => { await Promise.resolve() })

    expect(cb.onCancel).toHaveBeenCalledTimes(1)
    expect(cb.onDone).not.toHaveBeenCalled()
    expect(cb.onError).not.toHaveBeenCalled()
    // 取消按钮理应随 isTerminal 一起消失,不给用户点一个已经终态的任务。
    expect(screen.queryByRole('button', { name: /取消/ })).toBeNull()
  })

  it('卸载后清定时器,不再继续轮询', async () => {
    vi.mocked(api.getPrediction).mockResolvedValue(pred({ status: 'starting' }))

    const { unmount } = render(<AsyncRunState predictionId="p1" {...callbacks()} />)
    await act(async () => { await Promise.resolve() })
    const callsBeforeUnmount = vi.mocked(api.getPrediction).mock.calls.length

    unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })

    expect(vi.mocked(api.getPrediction).mock.calls.length).toBe(callsBeforeUnmount)
  })
})
