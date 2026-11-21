import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { useRestartComfy } from './comfyTemplates'

vi.mock('./client', () => ({ apiFetch: vi.fn() }))

describe('useRestartComfy', () => {
  it('waits for a fresh, ready process with a different PID', async () => {
    vi.useFakeTimers()
    try {
      const fetch = vi.mocked(apiFetch)
      fetch.mockResolvedValueOnce({ previous_pid: 42, requested_at: 10, was_online: true })
        .mockResolvedValueOnce({ ready: true, main_pid: 42, checked_at: 11 })
        .mockResolvedValueOnce({ ready: true, main_pid: 43, checked_at: 9 })
        .mockResolvedValueOnce({ ready: false, main_pid: 43, checked_at: 12 })
        .mockResolvedValueOnce({ ready: true, main_pid: 43, checked_at: 13 })
      const client = new QueryClient()
      const { result } = renderHook(() => useRestartComfy(), {
        wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
      })

      let verification!: Promise<boolean>
      await act(async () => {
        verification = result.current.mutateAsync()
        await vi.advanceTimersByTimeAsync(4_001)
      })
      expect(result.current.isPending).toBe(true)
      expect(fetch).toHaveBeenCalledTimes(4)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
        await expect(verification).resolves.toBe(true)
      })
      expect(fetch).toHaveBeenCalledTimes(5)
    } finally {
      vi.useRealTimers()
    }
  })
})
