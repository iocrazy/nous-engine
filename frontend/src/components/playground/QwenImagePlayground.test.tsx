import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

it('keeps the run action outside the scrollable parameter region', () => {
  render(<QwenImagePlayground inputs={fields} running={false} onRun={vi.fn()} />)
  const scroll = screen.getByRole('region', { name: '运行参数列表' })
  expect(scroll.contains(screen.getByText('高级参数'))).toBe(true)
  expect(scroll.contains(screen.getByRole('button', { name: '生成图片' }))).toBe(false)
  expect(scroll.contains(screen.getByRole('region', { name: '图片预览' }))).toBe(false)
})

it('groups each LoRA with its strength and keeps seed outside the LoRA group', () => {
  render(<QwenImagePlayground running={false} onRun={vi.fn()} inputs={[
    { node_id: 'bridge', key: 'lora_01', label: '编辑 LoRA 1', type: 'string' },
    ...fields,
    { node_id: 'bridge', key: 'lora_05', label: '编辑 LoRA 5', type: 'string' },
    { node_id: 'bridge', key: 'strength_01', label: 'LoRA 1 强度', type: 'number' },
    { node_id: 'bridge', key: 'strength_05', label: 'LoRA 5 强度', type: 'number' },
  ]} />)
  fireEvent.click(screen.getByText('高级参数'))
  const loras = within(screen.getByRole('group', { name: 'LoRA' }))
  const labels = ['编辑 LoRA 1', 'LoRA 1 强度', '编辑 LoRA 5', 'LoRA 5 强度'].map(label => loras.getByText(label))
  expect(labels.every((label, index) => index === 0 ||
    labels[index - 1].compareDocumentPosition(label) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true)
  expect(loras.queryByLabelText('种子')).not.toBeInTheDocument()
})

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

it('漫｜真互转待接入时不提交任务或改变工作流模式', () => {
  const onRun = vi.fn()
  const onModeChange = vi.fn()
  const { container } = render(<QwenImagePlayground inputs={fields}
    initialInputs={{ seed: 42 }} resultUrl="/result.png" running={false} onRun={onRun} onModeChange={onModeChange} />)

  fireEvent.click(screen.getByRole('radio', { name: '漫｜真互转' }))
  expect(screen.getByRole('radio', { name: '漫｜真互转' })).toBeChecked()
  expect(screen.getByText('等待 ComfyUI 工作流接入')).toBeVisible()
  expect(screen.queryByRole('button', { name: '生成图片' })).not.toBeInTheDocument()
  fireEvent.submit(container.querySelector('form')!)
  expect(onRun).not.toHaveBeenCalled()
  expect(onModeChange).not.toHaveBeenCalled()

  fireEvent.click(screen.getByRole('radio', { name: 'text' }))
  expect(screen.getByRole('link', { name: '下载结果' })).toHaveAttribute('href', '/result.png')
  fireEvent.click(screen.getByRole('button', { name: '生成图片' }))
  expect(onRun).toHaveBeenCalledWith({ input_mode: 'text', seed: 42 })
})

it('工作流提供漫｜真互转模式后，使用后端模式提交', () => {
  const onRun = vi.fn()
  const configured = [{ ...fields[0], constraints: { enum: ['text', 'comic_real'] } }, ...fields.slice(1)]
  render(<QwenImagePlayground inputs={configured} initialInputs={{ seed: 42 }} running={false} onRun={onRun} />)
  fireEvent.click(screen.getByRole('radio', { name: '漫｜真互转' }))
  fireEvent.click(screen.getByRole('button', { name: '转换图片' }))
  expect(onRun).toHaveBeenCalledWith({ input_mode: 'comic_real', seed: 42 })
})

it('可粘贴原图和选中的遮罩，并沿用任务上传参数', async () => {
  const onRun = vi.fn()
  const withMask = [...fields, {
    node_id: 'bridge', key: 'mask', type: 'image', required: true,
    constraints: { visible_when: { input_mode: 'remove' } },
  }]
  render(<QwenImagePlayground inputs={withMask} initialInputs={{ input_mode: 'remove', seed: 42 }}
    running={false} onRun={onRun} />)

  const original = new File(['original'], 'screenshot.png', { type: 'image/png' })
  fireEvent.paste(document.body, { clipboardData: { files: [original], items: [] } })
  await waitFor(() => expect(screen.getByRole('button', { name: '移除原图' })).toBeVisible())

  const mask = new File(['mask'], 'mask.png', { type: 'image/png' })
  fireEvent.paste(screen.getByRole('button', { name: /^遮罩\s*必填/ }), {
    clipboardData: { files: [], items: [{ kind: 'file', type: 'image/png', getAsFile: () => mask }] },
  })
  await waitFor(() => expect(screen.getByRole('button', { name: '移除遮罩' })).toBeVisible())

  fireEvent.click(screen.getByRole('button', { name: '移除遮罩区域' }))
  expect(onRun).toHaveBeenCalledWith({
    input_mode: 'remove', seed: 42,
    image: 'data:image/png;base64,b3JpZ2luYWw=', mask: 'data:image/png;base64,bWFzaw==',
  })
})
