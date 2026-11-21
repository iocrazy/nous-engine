import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import * as comfyTemplates from '../../api/comfyTemplates'
import { useImageTasks } from '../../api/tasks'
import type { ExposedParam } from '../../api/services'
import QwenImagePlayground from './QwenImagePlayground'

vi.mock('../../api/tasks', () => ({ useImageTasks: vi.fn() }))

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
  fireEvent.click(screen.getByRole('button', { name: '高级参数' }))
  expect(scroll.contains(screen.getByRole('spinbutton', { name: '种子' }))).toBe(true)
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
  fireEvent.click(mode)
  fireEvent.click(screen.getByRole('option', { name: '递增' }))
  fireEvent.click(run)
  fireEvent.click(run)
  fireEvent.click(mode)
  fireEvent.click(screen.getByRole('option', { name: '递减' }))
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
  expect(within(screen.getByRole('radiogroup', { name: '功能选项' })).getAllByRole('radio')).toHaveLength(2)
  expect(screen.queryByText('运行参数')).not.toBeInTheDocument()
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
  fireEvent.click(screen.getByRole('button', { name: '高级参数' }))
  expect(screen.getByRole('combobox', { name: '提示词优化模型' })).toHaveTextContent('本机 Qwen')
  fireEvent.click(screen.getByRole('checkbox', { name: '优化提示词' }))
  expect(screen.queryByRole('combobox', { name: '提示词优化模型' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('radio', { name: '图片反推' }))
  assertPlacement()
  fireEvent.click(screen.getByRole('button', { name: '高级参数' }))
  const provider = screen.getByRole('combobox', { name: '提示词优化模型' })
  fireEvent.click(provider)
  fireEvent.click(screen.getByRole('option', { name: 'DeepSeek' }))
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
  fireEvent.click(screen.getByRole('button', { name: '全屏预览' }))
  expect(screen.getByRole('region', { name: '图片预览' })).toHaveClass('qwen-preview-expanded')
  fireEvent.change(divider, { target: { value: '65' } })
  expect(screen.getByAltText('原图')).toHaveStyle({ clipPath: 'inset(0 35% 0 0)' })
  fireEvent.click(screen.getByRole('button', { name: '退出全屏' }))
  expect(divider).toHaveValue('65')
  fireEvent.click(screen.getByRole('button', { name: '并排' }))
  expect(screen.queryByRole('slider', { name: '拖动分隔线比较原图与结果' })).not.toBeInTheDocument()
})

it('shows the decoded dimensions and pixel count beneath the generated image', () => {
  render(<QwenImagePlayground inputs={fields} resultUrl="/result.png" running={false} onRun={vi.fn()} />)
  const result = screen.getByAltText('生成结果')
  Object.defineProperties(result, { naturalWidth: { value: 1920 }, naturalHeight: { value: 1080 } })
  fireEvent.load(result)
  expect(screen.getByLabelText('生成图片分辨率')).toHaveTextContent('1920 × 1080 px · 2.07 MP')
})

it('漫｜真互转待接入时不提交任务或改变工作流模式', () => {
  const onRun = vi.fn()
  const onModeChange = vi.fn()
  const { container } = render(<QwenImagePlayground inputs={fields}
    initialInputs={{ seed: 42 }} resultUrl="/result.png" running={false} onRun={onRun} onModeChange={onModeChange} />)

  fireEvent.click(screen.getByRole('button', { name: '单图／多图编辑' }))
  expect(screen.getByRole('button', { name: '单图／多图编辑' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByText('等待 ComfyUI 工作流接入')).toBeVisible()
  expect(screen.queryByRole('button', { name: '生成图片' })).not.toBeInTheDocument()
  fireEvent.submit(container.querySelector('form')!)
  expect(onRun).not.toHaveBeenCalled()
  expect(onModeChange).not.toHaveBeenCalled()

  fireEvent.click(screen.getByRole('button', { name: '生图与反推' }))
  expect(screen.getByRole('link', { name: '下载结果' })).toHaveAttribute('href', '/result.png')
  fireEvent.click(screen.getByRole('button', { name: '生成图片' }))
  expect(onRun).toHaveBeenCalledWith({ input_mode: 'text', seed: 42 })
})

it('工作流提供漫｜真互转模式后，使用后端模式提交', () => {
  const onRun = vi.fn()
  const configured = [{ ...fields[0], constraints: { enum: ['text', 'comic_real'] } }, ...fields.slice(1)]
  render(<QwenImagePlayground inputs={configured} initialInputs={{ seed: 42 }} running={false} onRun={onRun} />)
  fireEvent.click(screen.getByRole('button', { name: '单图／多图编辑' }))
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

it('adds, configures, and removes local LoRA rows and submits the list', async () => {
  const onRun = vi.fn()
  const files = vi.spyOn(comfyTemplates, 'getComfyLoras')
    .mockResolvedValue(['qwen2.1/first.safetensors', 'qwen2.1/second.safetensors', 'moss/other.safetensors'])
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  try {
    render(<QueryClientProvider client={client}>
      <QwenImagePlayground running={false} onRun={onRun} inputs={[
        ...fields,
        { node_id: 'bridge', key: 'qwen_loras', label: 'Qwen 生图 LoRA', type: 'lora_list' },
      ]} />
    </QueryClientProvider>)
    fireEvent.click(screen.getByRole('button', { name: '高级参数' }))
    const add = screen.getByRole('button', { name: '添加 LoRA' })
    await waitFor(() => expect(add).toBeEnabled())
    fireEvent.click(add)
    fireEvent.click(add)
    fireEvent.click(screen.getByRole('combobox', { name: 'LoRA 2 文件' }))
    fireEvent.click(screen.getByRole('option', { name: 'qwen2.1/second.safetensors' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '启用 LoRA 2' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: 'LoRA 2 强度' }), { target: { value: '0.75' } })
    fireEvent.click(screen.getByRole('button', { name: '移除 LoRA 1' }))
    expect(screen.queryByRole('option', { name: 'moss/other.safetensors' })).not.toBeInTheDocument()
    const syncedFile = 'qwen2.1/added.safetensors'
    files.mockResolvedValueOnce(['qwen2.1/first.safetensors', 'qwen2.1/second.safetensors', syncedFile, 'moss/other.safetensors'])
    fireEvent.click(screen.getByRole('button', { name: '同步目录' }))
    expect(await screen.findByText('已同步 · 3 个文件')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('combobox', { name: 'LoRA 1 文件' }))
    expect(screen.getByRole('option', { name: syncedFile })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'qwen2.1/second.safetensors' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('option', { name: 'moss/other.safetensors' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '生成图片' }))
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      qwen_loras: [{ lora: 'qwen2.1/second.safetensors', on: true, strength: 0.75 }],
    }))
  } finally {
    files.mockRestore()
  }
})

it('compares the selected reference rather than always using the first image', () => {
  render(<QwenImagePlayground inputs={[
    { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'edit' },
    { node_id: 'bridge', key: 'image', type: 'image' },
    { node_id: 'bridge', key: 'image_2', type: 'image' },
  ]} initialInputs={{ image: '/first.png', image_2: '/second.png' }}
    resultUrl="/result.png" running={false} onRun={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '对照' }))
  fireEvent.click(screen.getByRole('button', { name: '滑动对比' }))
  fireEvent.click(screen.getByRole('button', { name: '参考图 2' }))
  expect(screen.getByRole('button', { name: '对照' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByAltText('原图')).toHaveAttribute('src', '/second.png')
  fireEvent.click(screen.getByRole('button', { name: '并排' }))
  expect(screen.getByAltText('原图')).toHaveAttribute('src', '/second.png')
})

it('本地拖放到指定槽位，再从图库将原图添加到另一个槽位', async () => {
  const url = '/api/v1/tasks/gallery/100/image/0'
  vi.mocked(useImageTasks).mockReturnValue({
    data: { pages: [{ items: [{ kind: 'image', workflow_name: '图库任务', output_thumbnails: [url] }] }] },
  } as ReturnType<typeof useImageTasks>)
  const fetchOriginal = vi.spyOn(window, 'fetch').mockResolvedValue({
    ok: true, headers: new Headers({ 'content-type': 'image/png' }),
    blob: async () => new Blob(['original'], { type: 'image/png' }),
  } as Response)
  const dialogMethods = {
    showModal(this: HTMLDialogElement) { this.open = true },
    close(this: HTMLDialogElement) { this.open = false },
  }
  const dialogPrototype = HTMLDialogElement.prototype
  const descriptors = Object.getOwnPropertyDescriptors(dialogPrototype)
  for (const [key, value] of Object.entries(dialogMethods)) {
    Object.defineProperty(dialogPrototype, key, { configurable: true, value })
  }
  const onRun = vi.fn()
  try {
    render(<QwenImagePlayground inputs={[
      { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'edit' },
      { node_id: 'bridge', key: 'image', type: 'image' },
      { node_id: 'bridge', key: 'image_2', type: 'image' },
    ]} running={false} onRun={onRun} />)
    const target = screen.getByRole('button', { name: '参考图 2' })
    fireEvent.drop(target, {
      dataTransfer: { files: [new File(['reference'], 'ref.png', { type: 'image/png' })], types: ['Files'] },
    })
    await screen.findByRole('button', { name: '移除参考图 2' })
    fireEvent.click(within(screen.getByRole('region', { name: '素材' })).getByRole('button', { name: '原图' }))
    fireEvent.click(screen.getByRole('button', { name: '从图库选择' }))
    const picker = within(screen.getByRole('dialog', { name: '从图库选择' }))
    expect(picker.getByText('添加到原图')).toBeVisible()
    const option = picker.getByRole('button', { name: '图库任务 · 图片 1' })
    expect(option.querySelector('img')).toHaveAttribute('src', url + '?preview=1')
    fireEvent.click(option)
    fireEvent.click(picker.getByRole('button', { name: '使用图片' }))
    await screen.findByRole('button', { name: '移除原图' })
    expect(fetchOriginal).toHaveBeenCalledWith(url, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    fireEvent.click(screen.getByRole('button', { name: '编辑图片' }))
    expect(onRun).toHaveBeenCalledWith({
      input_mode: 'edit', image: 'data:image/png;base64,b3JpZ2luYWw=', image_2: 'data:image/png;base64,cmVmZXJlbmNl',
    })
  } finally {
    fetchOriginal.mockRestore()
    for (const key of Object.keys(dialogMethods)) {
      if (descriptors[key]) Object.defineProperty(dialogPrototype, key, descriptors[key])
      else Reflect.deleteProperty(dialogPrototype, key)
    }
  }
})

it('用一个模型下拉切换文生图，反推保持独立选项并同步预览标注', () => {
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={[
    { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'moss_text',
      constraints: { enum: ['moss_text', 'text', 'reference', 'oldx', 'character_card'] } },
    { node_id: 'bridge', key: 'reference_engine', type: 'string', default: 'qwen',
      constraints: { visible_when: { input_mode: 'reference' }, option_meta: [
        { value: 'qwen', label: 'Qwen Image 2.1' }, { value: 'moss', label: 'MOSS Image' },
      ] } },
  ]} running={false} onRun={onRun} />)
  const preview = within(screen.getByRole('region', { name: '图片预览' }))
  expect(preview.getByLabelText('当前模型 MOSS Image')).toBeVisible()
  const model = screen.getByRole('combobox', { name: '模型' })
  expect(model).toHaveTextContent('MOSS Image')
  fireEvent.click(model)
  fireEvent.click(screen.getByRole('option', { name: 'Qwen Image 2.1 Turbo' }))
  expect(preview.getByLabelText('当前模型 Qwen Image 2.1 Turbo')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '生成图片' }))
  expect(onRun).toHaveBeenLastCalledWith({ input_mode: 'text' })

  fireEvent.click(screen.getByRole('radio', { name: '图片反推' }))
  const reference = screen.getByRole('combobox', { name: '反推后使用的生图模型' })
  expect(reference).toHaveTextContent('Qwen Image 2.1 Turbo')
  fireEvent.click(reference)
  fireEvent.click(screen.getByRole('option', { name: 'MOSS Image' }))
  expect(preview.getByLabelText('当前模型 MOSS Image')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '反推并生图' }))
  expect(onRun).toHaveBeenLastCalledWith({ input_mode: 'reference', reference_engine: 'moss' })

  fireEvent.click(screen.getByRole('radio', { name: '文字生图' }))
  expect(screen.getByRole('combobox', { name: '模型' })).toHaveTextContent('Qwen Image 2.1 Turbo')
  fireEvent.click(screen.getByRole('button', { name: '人物工具' }))
  expect(preview.getByLabelText('当前模型 Qwen Image 2.1')).toBeVisible()
})
