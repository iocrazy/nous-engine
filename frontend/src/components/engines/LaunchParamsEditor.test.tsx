import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { LaunchParamsEditor, sanitizeLaunchParams } from './LaunchParamsEditor'

const mutate = vi.fn()

vi.mock('../../api/vllm', () => ({
  useUpdateLaunchParams: () => ({ mutate, isPending: false }),
}))

beforeEach(() => mutate.mockClear())

const CURRENT = {
  max_model_len: 262144,
  max_num_seqs: 8,
  max_num_batched_tokens: 8192,
  enable_prefix_caching: true,
}

// vLLM 类引擎的 editable(后端按适配器 __init__ 的形参表算出来的全集)。
const ALL = [
  'max_model_len',
  'max_num_seqs',
  'max_num_batched_tokens',
  'enable_prefix_caching',
  'dtype',
  'quantization',
]

describe('sanitizeLaunchParams', () => {
  it('丢掉非正数的数字键 —— `Number("")` 是 0,清空输入框不能把 0 写进 DB', () => {
    expect(sanitizeLaunchParams({ max_model_len: 0 })).toEqual({})
    expect(sanitizeLaunchParams({ max_num_seqs: -1 })).toEqual({})
    expect(sanitizeLaunchParams({ max_num_batched_tokens: NaN })).toEqual({})
    expect(sanitizeLaunchParams({ max_model_len: 1.5 })).toEqual({})
  })

  it('正整数原样放行,非数字键不受影响', () => {
    expect(
      sanitizeLaunchParams({ max_model_len: 32768, enable_prefix_caching: false }),
    ).toEqual({ max_model_len: 32768, enable_prefix_caching: false })
  })
})

describe('LaunchParamsEditor', () => {
  it('清空输入框 → 该键从 draft 里删掉(回落 current),不会存成 0', () => {
    render(<LaunchParamsEditor engineName="m" current={CURRENT} editable={ALL} overridden={[]} />)
    const input = screen.getByLabelText('max_num_seqs') as HTMLInputElement

    fireEvent.change(input, { target: { value: '' } })

    // 回落到 current,而不是显示 0
    expect(input.value).toBe('8')
    expect(screen.getByText('保存')).toBeDisabled()
  })

  it('输入正整数 → 保存发出去的就是它', () => {
    render(<LaunchParamsEditor engineName="m" current={CURRENT} editable={ALL} overridden={[]} />)
    fireEvent.change(screen.getByLabelText('max_num_seqs'), {
      target: { value: '16' },
    })
    fireEvent.click(screen.getByText('保存'))
    expect(mutate).toHaveBeenCalledWith({ name: 'm', body: { max_num_seqs: 16 } })
  })

  it('I5:先在输入框敲值、再点预设,预设不会被随后的"保存"悄悄回滚', () => {
    render(<LaunchParamsEditor engineName="m" current={CURRENT} editable={ALL} overridden={[]} />)
    // 用户先敲了个值(只进 draft,没发请求)
    fireEvent.change(screen.getByLabelText('max_model_len'), {
      target: { value: '4096' },
    })
    // 再点 256K 预设(立即发请求)
    fireEvent.click(screen.getByText('256K'))
    expect(mutate).toHaveBeenCalledWith({
      name: 'm',
      body: { max_model_len: 262144 },
    })

    // 预设已经把该键从 draft 里摘掉 → 没有别的待存项,"保存"按钮是灰的,
    // 不可能再把 draft 里那个 4096 发出去覆盖刚存的 256K。
    expect(screen.getByText('保存')).toBeDisabled()
    expect(mutate).toHaveBeenCalledTimes(1)
  })

  it('I5:勾选 prefix caching 同样即时保存 + 摘掉自己的 draft 键', () => {
    render(<LaunchParamsEditor engineName="m" current={CURRENT} editable={ALL} overridden={[]} />)
    const box = screen.getByRole('checkbox')
    fireEvent.click(box)
    expect(mutate).toHaveBeenCalledWith({
      name: 'm',
      body: { enable_prefix_caching: false },
    })
    expect(screen.getByText('保存')).toBeDisabled()
  })

  it('N1:不在 editable 里的键不渲染控件 —— 适配器吃不下,渲染了就是无效按钮', () => {
    render(
      <LaunchParamsEditor
        engineName="m"
        current={CURRENT}
        editable={['max_model_len']}
        overridden={[]}
      />,
    )
    expect(screen.getByLabelText('max_model_len')).toBeInTheDocument()
    expect(screen.queryByLabelText('max_num_seqs')).toBeNull()
    expect(screen.queryByLabelText('max_num_batched_tokens')).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('N5:overridden 里的键有「恢复默认」,点击发 null(三态里的"清除覆盖")', () => {
    render(
      <LaunchParamsEditor
        engineName="m"
        current={CURRENT}
        editable={ALL}
        overridden={['max_model_len']}
      />,
    )
    fireEvent.click(screen.getByLabelText('恢复默认 max_model_len'))
    expect(mutate).toHaveBeenCalledWith({
      name: 'm',
      body: { max_model_len: null },
    })
    // 只给被覆盖的那个键,没被覆盖的键本来就是 yaml 默认,没有可恢复的
    expect(screen.queryByLabelText('恢复默认 max_num_seqs')).toBeNull()
  })

  it('N5:overridden 为空 → 一个「恢复默认」都不渲染', () => {
    render(
      <LaunchParamsEditor engineName="m" current={CURRENT} editable={ALL} overridden={[]} />,
    )
    expect(screen.queryAllByText('恢复默认')).toHaveLength(0)
  })

  it('N5:「恢复默认」也要摘掉自己的 draft 键,免得随后的"保存"把值写回去', () => {
    render(
      <LaunchParamsEditor
        engineName="m"
        current={CURRENT}
        editable={ALL}
        overridden={['max_num_seqs']}
      />,
    )
    fireEvent.change(screen.getByLabelText('max_num_seqs'), { target: { value: '99' } })
    fireEvent.click(screen.getByLabelText('恢复默认 max_num_seqs'))
    expect(mutate).toHaveBeenCalledWith({ name: 'm', body: { max_num_seqs: null } })
    expect(screen.getByText('保存')).toBeDisabled()
    expect(mutate).toHaveBeenCalledTimes(1)
  })

  it('预设不吃掉输入框里**别的**键的待存值', () => {
    render(<LaunchParamsEditor engineName="m" current={CURRENT} editable={ALL} overridden={[]} />)
    fireEvent.change(screen.getByLabelText('max_num_seqs'), {
      target: { value: '4' },
    })
    fireEvent.click(screen.getByText('32K'))
    mutate.mockClear()

    fireEvent.click(screen.getByText('保存'))
    expect(mutate).toHaveBeenCalledWith({ name: 'm', body: { max_num_seqs: 4 } })
  })
})
