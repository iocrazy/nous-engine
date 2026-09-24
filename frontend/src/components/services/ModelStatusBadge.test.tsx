import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { EngineInfo } from '../../api/engines'

let engines: Partial<EngineInfo>[] = []
vi.mock('../../api/engines', () => ({ useEngines: () => ({ data: engines }) }))
vi.mock('../../api/components', () => ({
  useAllComponentStates: () => ({ data: [] }),
  loadedStateByFile: () => ({}),
}))

import ModelStatusBadge from './ModelStatusBadge'

const ref = (engine_key: string) => ({ kind: 'engine', engine_key }) as never

beforeEach(() => {
  engines = []
})

describe('ModelStatusBadge', () => {
  it('已加载 → 写明落在哪张卡', () => {
    engines = [{ name: 'huihui', status: 'loaded', loaded_gpu: 1, status_detail: null }]
    render(<ModelStatusBadge models={[ref('huihui')]} />)
    expect(screen.getByText('已加载 · GPU 1')).toBeTruthy()
  })

  it('未加载(如被 TTL 卸载)→ 明说调用会 503', () => {
    engines = [{ name: 'huihui', status: 'unloaded', loaded_gpu: null, status_detail: null }]
    render(<ModelStatusBadge models={[ref('huihui')]} />)
    const el = screen.getByText('未加载')
    expect(el.closest('span[title]')?.getAttribute('title')).toContain('503 model_not_ready')
  })

  it('加载失败 → 悬停显示失败原因', () => {
    engines = [{ name: 'huihui', status: 'failed', loaded_gpu: null, status_detail: 'ValueError: KV 装不下' }]
    render(<ModelStatusBadge models={[ref('huihui')]} />)
    const el = screen.getByText('加载失败')
    expect(el.closest('span[title]')?.getAttribute('title')).toContain('KV 装不下')
  })

  it('多模型服务 → 模型 x/y', () => {
    engines = [
      { name: 'a', status: 'loaded', loaded_gpu: 0, status_detail: null },
      { name: 'b', status: 'unloaded', loaded_gpu: null, status_detail: null },
    ]
    render(<ModelStatusBadge models={[ref('a'), ref('b')]} />)
    expect(screen.getByText('模型 1/2')).toBeTruthy()
  })

  it('没有模型引用 → 不渲染', () => {
    const { container } = render(<ModelStatusBadge models={[]} />)
    expect(container.innerHTML).toBe('')
  })
})
