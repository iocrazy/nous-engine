import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LaunchParamsStatus } from './LaunchParamsStatus'

const base = { effective: {}, defaults: {}, overridden: [] as string[] }

describe('LaunchParamsStatus', () => {
  it('上次加载失败 → 显示原因;有覆盖时提示恢复默认', () => {
    render(
      <LaunchParamsStatus
        status="failed"
        statusDetail="vLLM failed to start: 退出码 1 · ValueError: ... max seq len (262144) ..."
        overridden={['max_model_len']}
        effective={{ max_model_len: 262144 }}
        defaults={{ max_model_len: 32768 }}
      />,
    )
    expect(screen.getByRole('alert').textContent).toContain('262144')
    expect(screen.getByRole('alert').textContent).toContain('恢复默认')
  })

  it('覆盖项与 yaml 默认对照显示(含 K 换算)', () => {
    render(
      <LaunchParamsStatus
        status="loaded"
        statusDetail={null}
        overridden={['max_model_len']}
        effective={{ max_model_len: 262144 }}
        defaults={{ max_model_len: 32768 }}
      />,
    )
    expect(screen.getByText(/262144\(256K\)/)).toBeTruthy()
    expect(screen.getByText(/yaml 默认 32768\(32K\)/)).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('没失败也没覆盖 → 什么都不渲染', () => {
    const { container } = render(<LaunchParamsStatus status="loaded" statusDetail={null} {...base} />)
    expect(container.innerHTML).toBe('')
  })

  it('failed 但没有 detail 不渲染空红框', () => {
    const { container } = render(<LaunchParamsStatus status="failed" statusDetail="" {...base} />)
    expect(container.innerHTML).toBe('')
  })
})
