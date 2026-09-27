import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useSetGpu, useGpuGroups } from './engines'

vi.mock('./client', () => ({ apiFetch: vi.fn() }))
import { apiFetch } from './client'

vi.mock('../stores/toast', () => ({
  useToastStore: { getState: () => ({ add: vi.fn() }) },
}))

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('GPU 分配 hooks（单卡 / 组）', () => {
  beforeEach(() => vi.clearAllMocks())

  it('useSetGpu 传 gpus 时走请求体 {gpus}（张量并行组）', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ applied: false })
    const { result } = renderHook(() => useSetGpu(), { wrapper: wrapper() })
    result.current.mutate({ name: 'qwen3-35b', gpus: [0, 2] })
    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    const [url, opts] = vi.mocked(apiFetch).mock.calls[0]
    expect(url).toBe('/api/v1/engines/qwen3-35b/gpu')
    expect((opts as any).method).toBe('PATCH')
    expect(JSON.parse((opts as any).body)).toEqual({ gpus: [0, 2] })
  })

  it('useSetGpu 传单卡时仍走老的查询参数路径', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ applied: false })
    const { result } = renderHook(() => useSetGpu(), { wrapper: wrapper() })
    result.current.mutate({ name: 'qwen3-35b', gpu: 1 })
    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    const [url, opts] = vi.mocked(apiFetch).mock.calls[0]
    expect(url).toBe('/api/v1/engines/qwen3-35b/gpu?gpu=1')
    expect((opts as any).body).toBeUndefined()
  })

  it('useSetGpu 只有一张卡的 gpus 退化成单卡路径（组至少两张）', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ applied: false })
    const { result } = renderHook(() => useSetGpu(), { wrapper: wrapper() })
    result.current.mutate({ name: 'm', gpu: 2, gpus: [2] })
    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    expect(vi.mocked(apiFetch).mock.calls[0][0]).toBe('/api/v1/engines/m/gpu?gpu=2')
  })

  it('useGpuGroups 读 /api/v1/gpu/groups', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ groups: [] })
    renderHook(() => useGpuGroups(), { wrapper: wrapper() })
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/v1/gpu/groups'))
  })
})
