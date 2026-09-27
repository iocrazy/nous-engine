import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ComfyTemplateEditor, { type ComfyTemplateEditorProps } from './ComfyTemplateEditor'
import * as api from '../../api/comfyTemplates'

// ComfyTemplateEditor 保存后要 qc.invalidateQueries(...)(见组件内注释),所以现在需要
// 一个真实 QueryClientProvider 才能渲染 —— 用真实 client 包一层 wrap()。
function renderEditor(props: ComfyTemplateEditorProps) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const spy = vi.spyOn(qc, 'invalidateQueries')
  const result = render(
    <QueryClientProvider client={qc}>
      <ComfyTemplateEditor {...props} />
    </QueryClientProvider>,
  )
  return { ...result, qc, invalidateSpy: spy }
}

// React Flow rendering is exercised separately (comfyGraphLayout unit tests + real
// usage precedent in WorkflowAppEditor); mock the graph child here so editor tests
// stay fast/robust in jsdom and focus on popover/table/save logic (per task brief).
vi.mock('./ComfyTemplateGraph', () => ({
  default: ({
    nodes,
    onNodeClick,
  }: {
    nodes: Array<{ id: string; class_type: string; invalidModel?: boolean }>
    onNodeClick: (id: string) => void
  }) => (
    <div data-testid="graph-mock">
      {nodes.map((n) => (
        <button
          key={n.id}
          type="button"
          onClick={() => onNodeClick(n.id)}
          data-invalid-model={n.invalidModel ? 'true' : 'false'}
        >
          {n.class_type} #{n.id}
        </button>
      ))}
    </div>
  ),
}))

vi.mock('../../api/comfyTemplates', () => ({
  getComfyTemplate: vi.fn(),
  getComfyHealth: vi.fn(),
  getObjectInfo: vi.fn(),
  putMapping: vi.fn(),
  reuploadComfyTemplate: vi.fn(),
}))

const WORKFLOW = {
  '1': { class_type: 'LoadImage', inputs: { image: 'photo.png' } },
  '2': { class_type: 'KSampler', inputs: { pixels: ['1', 0], steps: 20, seed: 42 } },
}

function baseDetail(exposed: api.ComfyExposedParam[] = []): api.ComfyTemplateDetail {
  return {
    id: '7',
    name: 'tpl',
    service_name: 'svc-7',
    workflow_json: WORKFLOW,
    exposed_params: exposed,
  }
}

beforeEach(() => {
  vi.mocked(api.getComfyTemplate).mockResolvedValue(baseDetail())
  vi.mocked(api.getComfyHealth).mockResolvedValue({
    online: true, queue_depth: 0, version: '0.3.1', base_url: 'http://x', timeout_s: 30, devices: [],
  })
  vi.mocked(api.getObjectInfo).mockResolvedValue({
    KSampler: {
      input: {
        required: {
          steps: ['INT', { default: 20, min: 1, max: 150 }],
          seed: ['INT', { default: 0, min: 0, max: 999999 }],
        },
      },
    },
  })
  vi.mocked(api.putMapping).mockResolvedValue({ ok: true })
  vi.mocked(api.reuploadComfyTemplate).mockResolvedValue({ stale_keys: [] })
})

describe('ComfyTemplateEditor', () => {
  it('渲染每个节点一张卡', async () => {
    renderEditor({ templateId: '7' })
    expect(await screen.findByText('LoadImage #1')).toBeInTheDocument()
    expect(screen.getByText('KSampler #2')).toBeInTheDocument()
  })

  it('点卡片开弹窗,列出该节点原始 inputs(跳过节点引用连线)', async () => {
    renderEditor({ templateId: '7' })
    fireEvent.click(await screen.findByText('KSampler #2'))
    expect(await screen.findByText('steps')).toBeInTheDocument()
    expect(screen.getByText('seed')).toBeInTheDocument()
    expect(screen.queryByText('pixels')).not.toBeInTheDocument()
  })

  it('弹窗内勾选暴露 → 左侧画布预览出现该控件', async () => {
    renderEditor({ templateId: '7' })
    fireEvent.click(await screen.findByText('KSampler #2'))
    const stepsRow = (await screen.findByText('steps')).closest('[data-input-row]') as HTMLElement
    fireEvent.click(within(stepsRow).getByRole('checkbox', { name: /暴露/ }))
    // 汇总表已删(与预览面板重复):勾选后字段直接出现在左侧预览里
    const preview = await screen.findByTestId('canvas-preview-panel')
    await waitFor(() => expect(within(preview).getByText('steps')).toBeInTheDocument())
  })

  it('object_info 预填数值类型 min/max(未勾选也先展示,勾选后才可编辑)', async () => {
    renderEditor({ templateId: '7' })
    fireEvent.click(await screen.findByText('KSampler #2'))
    const stepsRow = (await screen.findByText('steps')).closest('[data-input-row]') as HTMLElement
    expect(within(stepsRow).getByDisplayValue('1')).toBeInTheDocument() // min
    expect(within(stepsRow).getByDisplayValue('150')).toBeInTheDocument() // max
  })

  it('seed 类整数出「随机」勾选', async () => {
    renderEditor({ templateId: '7' })
    fireEvent.click(await screen.findByText('KSampler #2'))
    const seedRow = (await screen.findByText('seed')).closest('[data-input-row]') as HTMLElement
    expect(within(seedRow).getByRole('checkbox', { name: /随机/ })).toBeInTheDocument()
  })

  it('保存调用 putMapping,payload 形状正确(comfy_node_id/comfy_input)', async () => {
    renderEditor({ templateId: '7' })
    fireEvent.click(await screen.findByText('KSampler #2'))
    const stepsRow = (await screen.findByText('steps')).closest('[data-input-row]') as HTMLElement
    fireEvent.click(within(stepsRow).getByRole('checkbox', { name: /暴露/ }))
    fireEvent.click(screen.getByRole('button', { name: /保存配置/ }))
    await waitFor(() => expect(api.putMapping).toHaveBeenCalled())
    const [id, params] = vi.mocked(api.putMapping).mock.calls[0]
    expect(id).toBe('7')
    expect(params).toEqual([
      expect.objectContaining({
        key: 'steps',
        comfy_node_id: '2',
        comfy_input: 'steps',
        type: 'integer',
        min: 1,
        max: 150,
      }),
    ])
  })

  it('保存成功后 invalidate 服务详情 + 服务列表缓存,切「运行」才能看到新字段', async () => {
    const { invalidateSpy } = renderEditor({ templateId: '7', serviceId: 'svc-42' })
    fireEvent.click(await screen.findByText('KSampler #2'))
    const stepsRow = (await screen.findByText('steps')).closest('[data-input-row]') as HTMLElement
    fireEvent.click(within(stepsRow).getByRole('checkbox', { name: /暴露/ }))
    fireEvent.click(screen.getByRole('button', { name: /保存配置/ }))
    await waitFor(() => expect(api.putMapping).toHaveBeenCalled())
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['services'] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['service', 'svc-42'] })
  })

  it('C2/I1:选「图片/文件(上传)」类型 → putMapping payload 里 type=image 原样送出', async () => {
    renderEditor({ templateId: '7' })
    fireEvent.click(await screen.findByText('KSampler #2'))
    const stepsRow = (await screen.findByText('steps')).closest('[data-input-row]') as HTMLElement
    fireEvent.click(within(stepsRow).getByRole('checkbox', { name: /暴露/ }))
    fireEvent.change(within(stepsRow).getByRole('combobox'), { target: { value: 'image' } })
    fireEvent.click(screen.getByRole('button', { name: /保存配置/ }))
    await waitFor(() => expect(api.putMapping).toHaveBeenCalled())
    // .at(-1), not [0]: mocks aren't cleared between tests in this file (no
    // clearMocks in vitest config), and an earlier test also calls putMapping
    // — calls[0] would silently grab that other test's payload instead of ours.
    const [, params] = vi.mocked(api.putMapping).mock.calls.at(-1)!
    expect(params).toEqual([
      expect.objectContaining({ key: 'steps', type: 'image' }),
    ])
  })

  it('sidecar 离线 → 降级提示', async () => {
    vi.mocked(api.getComfyHealth).mockResolvedValue({
      online: false, queue_depth: 0, version: '', base_url: 'http://x', timeout_s: 30, devices: [],
    })
    renderEditor({ templateId: '7' })
    expect(await screen.findByText(/离线/)).toBeInTheDocument()
  })

  it('已保存的 exposed_params 回显进左侧画布预览', async () => {
    vi.mocked(api.getComfyTemplate).mockResolvedValue(baseDetail([
      { key: 'steps', label: '步数', type: 'integer', comfy_node_id: '2', comfy_input: 'steps', min: 1, max: 150 },
    ]))
    renderEditor({ templateId: '7' })
    const preview = await screen.findByTestId('canvas-preview-panel')
    await waitFor(() => expect(within(preview).getByText('步数')).toBeInTheDocument())
  })

  it('上传替换:重新上传后失效字段在上传区列出(汇总表删了,这条可见性不能一起丢)', async () => {
    vi.mocked(api.getComfyTemplate).mockResolvedValue(baseDetail([
      { key: 'gone', label: 'gone', type: 'string', comfy_node_id: '99', comfy_input: 'x' },
    ]))
    vi.mocked(api.reuploadComfyTemplate).mockResolvedValue({ stale_keys: ['gone'] })
    renderEditor({ templateId: '7' })
    await screen.findByTestId('comfy-reupload-input')
    expect(screen.queryByTestId('stale-keys-warning')).not.toBeInTheDocument()

    const file = new File([JSON.stringify(WORKFLOW)], 'wf.json', { type: 'application/json' })
    fireEvent.change(screen.getByTestId('comfy-reupload-input'), { target: { files: [file] } })
    await waitFor(() => expect(api.reuploadComfyTemplate).toHaveBeenCalledWith('7', WORKFLOW))
    await waitFor(() => {
      expect(screen.getByTestId('stale-keys-warning')).toHaveTextContent('gone')
    })
  })

  // 事故背景见 workflowModelCheck.ts 头注:导入的工作流可能带着别的机器冻结下来的模型
  // 文件名,本机取值表里没有 —— 节点卡要在图上直接标出来,不用等真跑一次才炸。
  it('模型引用不在 object_info 取值表里 → 节点卡标记 invalidModel(初始加载即计算)', async () => {
    vi.mocked(api.getObjectInfo).mockResolvedValue({
      LoadImage: { input: { required: { image: [['a.png', 'b.png']] } } },
    })
    renderEditor({ templateId: '7' })
    const btn = (await screen.findByText('LoadImage #1')).closest('button')!
    expect(btn).toHaveAttribute('data-invalid-model', 'true')
    // 值在取值表里的节点不受影响
    expect(screen.getByText('KSampler #2').closest('button')).toHaveAttribute('data-invalid-model', 'false')
  })

  it('重新上传修正后的工作流 → invalidModel 标记按新内容重新计算(清掉)', async () => {
    vi.mocked(api.getObjectInfo).mockResolvedValue({
      LoadImage: { input: { required: { image: [['a.png', 'b.png']] } } },
    })
    renderEditor({ templateId: '7' })
    expect((await screen.findByText('LoadImage #1')).closest('button')).toHaveAttribute('data-invalid-model', 'true')

    const fixedWorkflow = { ...WORKFLOW, '1': { class_type: 'LoadImage', inputs: { image: 'a.png' } } }
    const file = new File([JSON.stringify(fixedWorkflow)], 'wf.json', { type: 'application/json' })
    fireEvent.change(screen.getByTestId('comfy-reupload-input'), { target: { files: [file] } })
    await waitFor(() => expect(api.reuploadComfyTemplate).toHaveBeenCalledWith('7', fixedWorkflow))
    await waitFor(() => {
      expect(screen.getByText('LoadImage #1').closest('button')).toHaveAttribute('data-invalid-model', 'false')
    })
  })
})
