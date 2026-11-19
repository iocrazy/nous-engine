import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import type { ExposedParam } from '../../api/services'
import QwenImagePlayground from './QwenImagePlayground'

const fields: ExposedParam[] = [
  { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'text',
    constraints: { widget: 'radio', enum: ['text', 'remove'] } },
  { node_id: 'bridge', key: 'seed', type: 'integer', label: '种子',
    constraints: { min: 0, max: 100 } },
  { node_id: 'bridge', key: 'image', type: 'image', required: true,
    constraints: { visible_when: { input_mode: 'remove' } } },
]

it('运行时用已选种子，按递增或递减策略更新下一次的值', () => {
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={fields} initialInputs={{ seed: 42 }} running={false} onRun={onRun} />)
  fireEvent.click(screen.getByText('高级参数'))
  const mode = screen.getByLabelText('种子模式')
  const run = screen.getByRole('button', { name: '生成图片' })
  fireEvent.change(mode, { target: { value: 'increment' } })
  fireEvent.click(run)
  fireEvent.click(run)
  fireEvent.change(mode, { target: { value: 'decrement' } })
  fireEvent.click(run)
  expect(onRun.mock.calls.map(([input]) => input)).toEqual([
    { input_mode: 'text', seed: 42 },
    { input_mode: 'text', seed: 43 },
    { input_mode: 'text', seed: 44 },
  ])
  expect(screen.getByRole('spinbutton', { name: '种子' })).toHaveValue(43)
})

it('立即随机生成种子，运行时提交屏幕上显示的值', () => {
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={fields} running={false} onRun={onRun} />)
  fireEvent.click(screen.getByText('高级参数'))
  fireEvent.click(screen.getByRole('button', { name: '随机种子' }))
  const seed = (screen.getByRole('spinbutton', { name: '种子' }) as HTMLInputElement).valueAsNumber
  expect(seed).toBeGreaterThanOrEqual(0)
  expect(seed).toBeLessThanOrEqual(100)
  fireEvent.click(screen.getByRole('button', { name: '生成图片' }))
  expect(onRun).toHaveBeenCalledWith({ input_mode: 'text', seed })
})

it('OLDX 提示姿势与场景参考，人物资产卡沿用固定版式', () => {
  const assets: ExposedParam[] = [
    { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'oldx',
      constraints: { widget: 'radio', enum: ['oldx', 'character_card'], option_meta: [
        { value: 'oldx', label: 'OLDX 人物替换' },
        { value: 'character_card', label: '人物资产卡' },
      ] } },
    { node_id: 'bridge', key: 'image', type: 'image', required: true },
    { node_id: 'bridge', key: 'oldx_target', type: 'image', label: '姿势与场景参考',
      constraints: { visible_when: { input_mode: 'oldx' }, required_when: { input_mode: 'oldx' } } },
  ]
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={assets}
    initialInputs={{ input_mode: 'oldx', image: 'data:image/png;base64,AA==' }} running={false} onRun={onRun} />)
  expect(screen.getByRole('button', { name: /^人物源图/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /姿势与场景参考/ })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '替换人物' }))
  expect(onRun).not.toHaveBeenCalled()
  expect(screen.getByRole('status')).toHaveTextContent('姿势与场景参考')
  fireEvent.click(screen.getByRole('radio', { name: '人物资产卡' }))
  expect(screen.queryByRole('button', { name: /姿势与场景参考/ })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '生成人物资产卡' }))
  expect(onRun).toHaveBeenCalledWith({ input_mode: 'character_card', image: 'data:image/png;base64,AA==' })
})

it('明确显示优化模型并统一将高清放大放在尺寸参数之后', () => {
  const modes: ExposedParam[] = [
    { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'moss_text',
      constraints: { widget: 'radio', enum: ['moss_text', 'reference'] } },
    { node_id: 'bridge', key: 'enable_upscale', type: 'boolean', default: false, label: '处理后高清放大' },
    { node_id: 'bridge', key: 'enable_optimization', type: 'boolean', default: true, label: '优化提示词',
      constraints: { visible_when: { input_mode: 'moss_text' } } },
    { node_id: 'bridge', key: 'use_deepseek', type: 'boolean', default: false },
    { node_id: 'bridge', key: 'moss_megapixels', type: 'number', label: 'MOSS 像素', default: 2 },
  ]
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={modes} running={false} onRun={onRun} />)

  const assertPlacement = () => {
    const pixels = screen.getByText('MOSS 像素')
    const upscale = screen.getByText('处理后高清放大')
    expect(pixels.compareDocumentPosition(upscale) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  }
  assertPlacement()
  expect(screen.getByRole('combobox', { name: '提示词优化模型' })).toHaveValue('local')
  fireEvent.click(screen.getByRole('checkbox', { name: '优化提示词' }))
  expect(screen.queryByRole('combobox', { name: '提示词优化模型' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('radio', { name: 'reference' }))
  assertPlacement()
  const provider = screen.getByRole('combobox', { name: '提示词优化模型' })
  fireEvent.change(provider, { target: { value: 'deepseek' } })
  fireEvent.click(screen.getByRole('button', { name: '反推并生图' }))
  expect(onRun).toHaveBeenCalledWith(expect.objectContaining({ input_mode: 'reference', use_deepseek: true }))
})

it('对照可以在并排与滑动之间切换', () => {
  render(<QwenImagePlayground inputs={fields}
    initialInputs={{ input_mode: 'remove', image: 'data:image/png;base64,AA==' }}
    resultUrl="/result.png" running={false} onRun={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '对照' }))
  expect(screen.getByRole('button', { name: '并排' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(screen.getByRole('button', { name: '滑动对比' }))
  const divider = screen.getByRole('slider', { name: '拖动分隔线比较原图与结果' })
  fireEvent.change(divider, { target: { value: '72' } })
  expect(screen.getByAltText('原图')).toHaveStyle({ clipPath: 'inset(0 28% 0 0)' })
  fireEvent.click(screen.getByRole('button', { name: '并排' }))
  expect(screen.queryByRole('slider', { name: '拖动分隔线比较原图与结果' })).not.toBeInTheDocument()
})
