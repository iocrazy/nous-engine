import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import ImportComfyDialog from './ImportComfyDialog'
import * as api from '../../api/comfyTemplates'

vi.mock('../../api/comfyTemplates', () => ({
  createComfyTemplate: vi.fn(async () => ({ id: 1, service_name: 'nous-minimax-h3-r2v', node_count: 2 })),
  getObjectInfo: vi.fn(async () => ({})),
}))

beforeEach(() => {
  vi.mocked(api.createComfyTemplate).mockClear()
  vi.mocked(api.getObjectInfo).mockReset()
  vi.mocked(api.getObjectInfo).mockResolvedValue({})
})

// 事故复现:VAELoader.vae_name 冻结了另一台机器的裸文件名,本机取值表里只有带
// minimax-h3/ 子目录前缀的同名文件。
const OBJECT_INFO_WITH_VAE = {
  VAELoader: {
    input: {
      required: {
        vae_name: [['ae.safetensors', 'minimax-h3/minimax_h3_video_vae_fp16.safetensors']],
      },
    },
  },
}

function importFile(overrides?: { name?: string }) {
  const file = new File(
    [JSON.stringify({ '119': { class_type: 'VAELoader', inputs: { vae_name: 'minimax_h3_video_vae_fp16.safetensors' } } })],
    'wf.json',
    { type: 'application/json' },
  )
  fireEvent.change(screen.getByTestId('comfy-file-input'), { target: { files: [file] } })
  return { file, name: overrides?.name ?? 'nous-minimax-h3-r2v' }
}

describe('ImportComfyDialog', () => {
  it('拒绝非 API 格式 JSON', async () => {
    render(<ImportComfyDialog open onClose={() => {}} onImported={() => {}} />)
    const file = new File(['{"nodes":[]}'], 'ui.json', { type: 'application/json' })
    fireEvent.change(screen.getByTestId('comfy-file-input'), { target: { files: [file] } })
    await waitFor(() => expect(screen.getByText(/API 格式/)).toBeInTheDocument())
  })

  it('合法 JSON + 名称 + 无模型引用问题 → 提交回调服务名', async () => {
    const onImported = vi.fn()
    render(<ImportComfyDialog open onClose={() => {}} onImported={onImported} />)
    const file = new File([JSON.stringify({ '1': { class_type: 'X', inputs: {} } })], 'wf.json',
      { type: 'application/json' })
    fireEvent.change(screen.getByTestId('comfy-file-input'), { target: { files: [file] } })
    fireEvent.change(await screen.findByPlaceholderText(/服务名/), { target: { value: 'nous-minimax-h3-r2v' } })
    fireEvent.click(screen.getByRole('button', { name: /导入/ }))
    await waitFor(() => expect(onImported).toHaveBeenCalledWith('nous-minimax-h3-r2v'))
  })

  it('服务名缺 nous- 前缀 → 显示中文规则提示且不提交', async () => {
    const onImported = vi.fn()
    render(<ImportComfyDialog open onClose={() => {}} onImported={onImported} />)
    const file = new File([JSON.stringify({ '1': { class_type: 'X', inputs: {} } })], 'wf.json',
      { type: 'application/json' })
    fireEvent.change(screen.getByTestId('comfy-file-input'), { target: { files: [file] } })
    fireEvent.change(await screen.findByPlaceholderText(/服务名/), { target: { value: 'minimax-h3-r2v' } })
    expect(await screen.findByText(/必须以 nous- 开头/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^导入$/ }))
    await new Promise((r) => setTimeout(r, 50))
    expect(api.createComfyTemplate).not.toHaveBeenCalled()
    expect(onImported).not.toHaveBeenCalled()
  })

  it('sidecar 离线(getObjectInfo 失败)→ 跳过校验,照常导入', async () => {
    vi.mocked(api.getObjectInfo).mockRejectedValue(new Error('sidecar down'))
    const onImported = vi.fn()
    render(<ImportComfyDialog open onClose={() => {}} onImported={onImported} />)
    importFile()
    fireEvent.change(await screen.findByPlaceholderText(/服务名/), { target: { value: 'nous-minimax-h3-r2v' } })
    fireEvent.click(screen.getByRole('button', { name: /^导入$/ }))
    await waitFor(() => expect(onImported).toHaveBeenCalledWith('nous-minimax-h3-r2v'))
    // 导入时用的是原始(未经修正)workflow —— 校验被跳过,不是静默改数据
    const [, workflow] = vi.mocked(api.createComfyTemplate).mock.calls.at(-1)!
    expect((workflow as Record<string, { inputs: { vae_name: string } }>)['119'].inputs.vae_name).toBe(
      'minimax_h3_video_vae_fp16.safetensors',
    )
  })

  it('模型引用不合法 → 出 review 步,预选建议值;确认后用修正后的 workflow 导入', async () => {
    vi.mocked(api.getObjectInfo).mockResolvedValue(OBJECT_INFO_WITH_VAE)
    const onImported = vi.fn()
    render(<ImportComfyDialog open onClose={() => {}} onImported={onImported} />)
    importFile()
    fireEvent.change(await screen.findByPlaceholderText(/服务名/), { target: { value: 'nous-minimax-h3-r2v' } })
    fireEvent.click(screen.getByRole('button', { name: /^导入$/ }))

    // review 步出现,select 已预选建议值
    const select = await screen.findByRole('combobox', { name: /vae_name/ })
    await waitFor(() => expect(select).toHaveValue('minimax-h3/minimax_h3_video_vae_fp16.safetensors'))
    expect(api.createComfyTemplate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /确认修正并导入/ }))
    await waitFor(() => expect(onImported).toHaveBeenCalledWith('nous-minimax-h3-r2v'))
    const [name, workflow] = vi.mocked(api.createComfyTemplate).mock.calls.at(-1)!
    expect(name).toBe('nous-minimax-h3-r2v')
    expect((workflow as Record<string, { inputs: { vae_name: string } }>)['119'].inputs.vae_name).toBe(
      'minimax-h3/minimax_h3_video_vae_fp16.safetensors',
    )
  })

  it('review 步「全部采用建议」批量填充候选', async () => {
    vi.mocked(api.getObjectInfo).mockResolvedValue(OBJECT_INFO_WITH_VAE)
    render(<ImportComfyDialog open onClose={() => {}} onImported={() => {}} />)
    importFile()
    fireEvent.change(await screen.findByPlaceholderText(/服务名/), { target: { value: 'nous-minimax-h3-r2v' } })
    fireEvent.click(screen.getByRole('button', { name: /^导入$/ }))

    const select = await screen.findByRole('combobox', { name: /vae_name/ })
    // 手动清空成未选状态,再点「全部采用建议」验证它确实重新填充
    fireEvent.change(select, { target: { value: '' } })
    expect(select).toHaveValue('')
    fireEvent.click(screen.getByRole('button', { name: /全部采用建议/ }))
    expect(select).toHaveValue('minimax-h3/minimax_h3_video_vae_fp16.safetensors')
  })
})
