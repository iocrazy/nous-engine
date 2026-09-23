import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ServiceCapabilityChips from './ServiceCapabilityChips'
import { buildClientConfigText, formatContextK } from './serviceCapabilities'
import type { ServiceCapabilities } from '../../api/services'

const FULL: ServiceCapabilities = {
  context: 262144,
  max_output: null,
  tools: true,
  thinking: true,
  vision: true,
  provider: 'huihui-ai',
  source: 'huihui-ai/Huihui-Qwen3.8-27B-abliterated',
}

describe('formatContextK', () => {
  it('按 1024 换算成 K', () => {
    expect(formatContextK(262144)).toBe('256K')
    expect(formatContextK(32768)).toBe('32K')
    expect(formatContextK(8192)).toBe('8K')
    expect(formatContextK(12800)).toBe('12.5K')
    expect(formatContextK(512)).toBe('512')
  })
})

describe('ServiceCapabilityChips', () => {
  it('渲染全部能力,提示里写清思考字段与输出上限', () => {
    render(<ServiceCapabilityChips capabilities={FULL} />)
    const ctx = screen.getByText('256K 上下文')
    expect(ctx.getAttribute('title')).toContain('≤ 上下文 − 输入')
    expect(screen.getByText('工具')).toBeTruthy()
    expect(screen.getByText('思考').getAttribute('title')).toContain('reasoning_content')
    expect(screen.getByText('图片')).toBeTruthy()
    expect(screen.getByText('huihui-ai')).toBeTruthy()
  })

  it('没有的项不显示', () => {
    render(
      <ServiceCapabilityChips
        capabilities={{ ...FULL, context: 32768, tools: false, vision: false, provider: null, source: null }}
      />,
    )
    expect(screen.getByText('32K 上下文')).toBeTruthy()
    expect(screen.getByText('思考')).toBeTruthy()
    expect(screen.queryByText('工具')).toBeNull()
    expect(screen.queryByText('图片')).toBeNull()
    expect(screen.queryByText('huihui-ai')).toBeNull()
  })

  it('无 capabilities / 全空时不渲染', () => {
    const { container, rerender } = render(<ServiceCapabilityChips capabilities={null} />)
    expect(container.innerHTML).toBe('')
    rerender(<ServiceCapabilityChips />)
    expect(container.innerHTML).toBe('')
    rerender(
      <ServiceCapabilityChips
        capabilities={{
          context: null, max_output: null, tools: false, thinking: false,
          vision: false, provider: null, source: null,
        }}
      />,
    )
    expect(container.innerHTML).toBe('')
  })
})

describe('buildClientConfigText', () => {
  it('按客户端供应商表单逐项给出', () => {
    const text = buildClientConfigText({
      url: 'http://heygo-ubuntu:8000/v1/chat/completions',
      model: 'qwen3-8-27b-huihui',
      caps: { ...FULL, context: 32768, vision: false },
    })
    expect(text).toBe(
      [
        '接口地址: http://heygo-ubuntu:8000/v1/chat/completions',
        '模型名称: qwen3-8-27b-huihui',
        '工具调用: 是   图片输入: 否   思考模式: 是',
        '输入(上下文): 32K   输出: 使用默认(≤ 上下文)',
        '提供商: huihui-ai',
      ].join('\n'),
    )
  })
})
