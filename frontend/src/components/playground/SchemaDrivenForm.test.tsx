import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import SchemaDrivenForm from './SchemaDrivenForm'
import type { ExposedParam } from '../../api/services'

function setup(inputs: ExposedParam[]) {
  const onSubmit = vi.fn()
  render(<SchemaDrivenForm inputs={inputs} onSubmit={onSubmit} />)
  return { onSubmit }
}

function setupWithInitial(inputs: ExposedParam[], initialValues: Record<string, unknown>) {
  const onSubmit = vi.fn()
  render(<SchemaDrivenForm inputs={inputs} onSubmit={onSubmit} initialValues={initialValues} />)
  return { onSubmit }
}

describe('SchemaDrivenForm', () => {
  it('offers an upscale-only mode and submits only image and upscale controls', () => {
    const { onSubmit } = setupWithInitial([
      {
        node_id: 'bridge', key: 'input_mode', type: 'string', label: '输入方式',
        constraints: {
          widget: 'radio', enum: ['text', 'reference', 'upscale'],
          option_meta: [
            { value: 'text', label: '文字生图' },
            { value: 'reference', label: '图片反推' },
            { value: 'upscale', label: '仅图片放大' },
          ],
        },
      },
      { node_id: 'bridge', key: 'use_reference', type: 'boolean', default: false, constraints: { hidden: true } },
      { node_id: 'bridge', key: 'prompt', type: 'string', label: '提示词', default: '', constraints: { visible_when: { input_mode: 'text' } } },
      { node_id: 'bridge', key: 'lora', type: 'string', label: 'LoRA', default: 'moss', constraints: { visible_when: { input_mode: ['text', 'reference'] } } },
      { node_id: 'bridge', key: 'reference_image', type: 'image', label: '输入图片', constraints: { visible_when: { input_mode: ['reference', 'upscale'] } } },
      { node_id: 'bridge', key: 'upscale_resolution', type: 'int', label: '放大尺寸', default: 2560 },
    ], { reference_image: 'data:image/png;base64,aW1hZ2U=' })
    const mode = screen.getByRole('radiogroup', { name: '输入方式' })
    expect(within(mode).getByRole('radio', { name: '文字生图' })).toBeChecked()
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: '保留提示词' } })
    fireEvent.click(within(mode).getByRole('radio', { name: '仅图片放大' }))
    expect(screen.queryByText('提示词')).not.toBeInTheDocument()
    expect(screen.queryByText('LoRA')).not.toBeInTheDocument()
    expect(screen.getByText('输入图片')).toBeInTheDocument()
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenLastCalledWith({
      input_mode: 'upscale', reference_image: 'data:image/png;base64,aW1hZ2U=', upscale_resolution: 2560,
    })
    fireEvent.click(within(mode).getByRole('radio', { name: '文字生图' }))
    expect(screen.getAllByRole('textbox')[0]).toHaveValue('保留提示词')
  })

  it('switches exclusive input modes, retains values and omits inactive inputs', () => {
    const { onSubmit } = setup([
      {
        node_id: 'bridge', key: 'use_reference', type: 'boolean', label: '输入方式', default: false,
        constraints: {
          enum: [false, true],
          option_meta: [{ value: false, label: '文字生图' }, { value: true, label: '图片反推' }],
        },
      },
      {
        node_id: 'bridge', key: 'prompt', type: 'string', label: '提示词', default: '',
        constraints: { visible_when: { use_reference: false } },
      },
      {
        node_id: 'bridge', key: 'enable_expansion', type: 'boolean', label: '扩写', default: true,
        constraints: { visible_when: { use_reference: false } },
      },
      {
        node_id: 'bridge', key: 'reference_image', type: 'image', label: '参考图',
        constraints: { visible_when: { use_reference: true } },
      },
      {
        node_id: 'bridge', key: 'reverse_instruction', type: 'string', label: '反推要求', default: '描述图片',
        constraints: { visible_when: { use_reference: true } },
      },
    ])
    const mode = screen.getByRole('radiogroup', { name: '输入方式' })
    const text = within(mode).getByRole('radio', { name: '文字生图' })
    const reference = within(mode).getByRole('radio', { name: '图片反推' })
    expect(text).toBeChecked()
    expect(reference).not.toBeChecked()
    expect(screen.queryByText('参考图')).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '保存文字' } })
    fireEvent.click(reference)
    expect(reference).toBeChecked()
    expect(text).not.toBeChecked()
    expect(screen.queryByText('提示词')).not.toBeInTheDocument()
    expect(screen.queryByText('扩写')).not.toBeInTheDocument()
    expect(screen.getByText('参考图')).toBeInTheDocument()
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenLastCalledWith({
      use_reference: true, reference_image: null, reverse_instruction: '描述图片',
    })
    fireEvent.click(text)
    expect(screen.getByRole('textbox')).toHaveValue('保存文字')
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenLastCalledWith({ use_reference: false, prompt: '保存文字', enable_expansion: true })
  })

  it('restores the visible mode from historical values and applies all visibility conditions', () => {
    setupWithInitial([
      { node_id: 'b', key: 'use_reference', type: 'boolean', default: false },
      { node_id: 'b', key: 'expand', type: 'boolean', default: true },
      {
        node_id: 'b', key: 'temperature', type: 'number', label: '扩写温度',
        constraints: { visible_when: { use_reference: false, expand: true } },
      },
    ], { use_reference: true })
    expect(screen.queryByText('扩写温度')).not.toBeInTheDocument()
  })

  it('renders empty-state hint when no inputs', () => {
    setup([])
    expect(screen.getByText(/没有暴露入参/)).toBeInTheDocument()
  })

  it('renders a string input and submits its value', () => {
    const { onSubmit } = setup([
      { node_id: 'in_1', key: 'prompt', input_name: 'value', label: '提示词', type: 'string', required: true },
    ])
    const inp = screen.getByRole('textbox')
    fireEvent.change(inp, { target: { value: 'hello' } })
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenCalledWith({ prompt: 'hello' })
  })

  it('prefills from initialValues (重跑回填) overriding defaults', () => {
    const { onSubmit } = setupWithInitial(
      [{ node_id: 'in_1', key: 'prompt', input_name: 'value', label: '提示词', type: 'string' }],
      { prompt: '历史提示词' },
    )
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('历史提示词')
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenCalledWith({ prompt: '历史提示词' })
  })

  it('renders a multiline textarea for type=string_multiline', () => {
    setup([
      {
        node_id: 'in_2',
        key: 'script',
        input_name: 'value',
        label: '脚本',
        type: 'string_multiline',
      },
    ])
    expect(screen.getByRole('textbox').tagName).toBe('TEXTAREA')
  })

  it('renders a select when constraints.enum is present', () => {
    setup([
      {
        node_id: 'in_3',
        key: 'voice',
        input_name: 'value',
        label: '音色',
        type: 'string',
        constraints: { enum: ['alice', 'bob'] },
      },
    ])
    // NodeSelectPopover:点触发打开浮层,选项 alice/bob 出现。
    fireEvent.click(screen.getByText('请选择'))
    expect(screen.getByRole('option', { name: 'alice' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'bob' })).toBeInTheDocument()
  })

  it('renders a file upload control (not a select) for type=image even with constraints.enum present', () => {
    // 回归用例:ComfyUI LoadImage 节点经 /object_info 带出的 enum 是 sidecar 上
    // 已存在的文件名列表(如服务 minimax-h3-r2v 真实数据),不是可选值域 —— 文件语义
    // 必须优先于 enum,否则 Playground 渲不出上传控件。
    setup([
      {
        node_id: 'bridge', key: 'image', input_name: 'image', label: 'image', type: 'image',
        default: '5 (1).jpg', required: true,
        constraints: { enum: ['5 (1).jpg', 'example.png'] },
      },
    ])
    expect(screen.getByText(/点击或拖入选择文件/)).toBeInTheDocument()
    expect(screen.queryByText('请选择')).not.toBeInTheDocument()
  })

  it('renders a checkbox for boolean and forwards the typed value on submit', () => {
    const { onSubmit } = setup([
      { node_id: 'in_4', key: 'stream', input_name: 'value', label: '流式', type: 'boolean' },
    ])
    const cb = screen.getByRole('checkbox')
    fireEvent.click(cb)
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenCalledWith({ stream: true })
  })

  it('parses number input as a Number on submit', () => {
    const { onSubmit } = setup([
      { node_id: 'in_5', key: 'temperature', input_name: 'value', label: '温度', type: 'number' },
    ])
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '0.7' } })
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenCalledWith({ temperature: 0.7 })
  })

  it('renders a slider (range + number) when numeric with min/max', () => {
    const { onSubmit } = setup([
      {
        node_id: 'in_s', key: 'steps', input_name: 'steps', label: '步数',
        type: 'number', constraints: { min: 1, max: 100, step: 1 }, default: 20,
      },
    ])
    const range = screen.getByRole('slider') as HTMLInputElement
    expect(range).toBeInTheDocument()
    expect(range.min).toBe('1')
    expect(range.max).toBe('100')
    fireEvent.change(range, { target: { value: '30' } })
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenCalledWith({ steps: 30 })
  })

  it('shows a randomize button for seed-like numeric fields and sets a number', () => {
    const { onSubmit } = setup([
      { node_id: 'in_seed', key: 'seed', input_name: 'seed', label: '种子', type: 'integer' },
    ])
    fireEvent.click(screen.getByLabelText('随机种子'))
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenCalledTimes(1)
    const arg = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(typeof arg.seed).toBe('number')
  })

  it('uses enum_labels for select option text', () => {
    setup([
      {
        node_id: 'in_e', key: 'mode', input_name: 'mode', label: '模式', type: 'string',
        constraints: { enum: ['a', 'b'], enum_labels: { a: '甲', b: '乙' } },
      },
    ])
    // NodeSelectPopover:打开后选项文本用 enum_labels(甲/乙)。
    fireEvent.click(screen.getByText('请选择'))
    expect(screen.getByRole('option', { name: '甲' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '乙' })).toBeInTheDocument()
  })

  it('honors legacy aliases (api_name + param_key) on backfilled rows', () => {
    const { onSubmit } = setup([
      { node_id: 'in_6', api_name: 'prompt', param_key: 'value', label: '提示', type: 'string' },
    ])
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByText(/▶ 运行/))
    expect(onSubmit).toHaveBeenCalledWith({ prompt: 'hi' })
  })
})

describe('SchemaDrivenForm — 空数字字段不该发空串', () => {
  it('留空的可选整数字段要从 payload 里省略,而不是发 ""', () => {
    // 2026-08-31 实机:krea2 的 seed 是可选整数,UI 不填就发 {"seed": ""},
    // 后端 validate_service_input 判 "seed: expected integer" → 422,
    // Playground 的运行按钮对任何带可选空数字字段的服务直接不可用。
    const onSubmit = vi.fn()
    render(<SchemaDrivenForm inputs={[
      { node_id: 'bridge', key: 'prompt', type: 'string', required: false },
      { node_id: 'bridge', key: 'seed', type: 'integer', required: false },
    ] as ExposedParam[]} onSubmit={onSubmit} />)

    fireEvent.click(screen.getByRole('button', { name: /运行/ }))
    const payload = onSubmit.mock.calls[0][0]
    expect(payload).not.toHaveProperty('seed')
    // 字符串字段的空串是合法值(用户就是想传空提示词),要保留
    expect(payload).toHaveProperty('prompt', '')
  })

  it('填了值的数字字段照常提交为数字', () => {
    const onSubmit = vi.fn()
    render(<SchemaDrivenForm inputs={[
      { node_id: 'bridge', key: 'seed', type: 'integer', required: false },
    ] as ExposedParam[]} onSubmit={onSubmit} />)

    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '42' } })
    fireEvent.click(screen.getByRole('button', { name: /运行/ }))
    expect(onSubmit.mock.calls[0][0]).toEqual({ seed: 42 })
  })
})
