import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import DeleteModelDialog from './DeleteModelDialog'
import type { EngineInfo } from '../../api/engines'
import type { DeletePreflight } from '../../api/engines'

const preflightMock = vi.fn()
const deleteMutate = vi.fn()

vi.mock('../../api/engines', async () => {
  const actual = await vi.importActual<typeof import('../../api/engines')>('../../api/engines')
  return {
    ...actual,
    useDeletePreflight: () => preflightMock(),
    useDeleteEngine: () => ({ mutate: deleteMutate, isPending: false }),
  }
})

const ENGINE = {
  name: 'doomed_model',
  display_name: 'Doomed-Model',
  type: 'llm',
  status: 'unloaded',
  kind: 'model',
} as unknown as EngineInfo

function makePreflight(over: Partial<DeletePreflight> = {}): DeletePreflight {
  return {
    name: 'doomed_model',
    kind: 'model',
    target_path: '/models/nous/llm/Doomed-Model',
    is_dir: true,
    size_bytes: 1024 * 1024 * 1024,
    blockers: { loaded: null, services: [] },
    registry_cleanup: {
      models_d_yaml: 'configs/models.d/doomed_model.yaml',
      model_metadata: true,
      runtime_overrides: 1,
    },
    code_refs: [],
    code_refs_truncated: false,
    code_refs_error: null,
    ...over,
  }
}

function setPreflight(data: DeletePreflight | null, isLoading = false) {
  preflightMock.mockReturnValue({ data, isLoading, isError: false })
}

describe('DeleteModelDialog', () => {
  beforeEach(() => {
    preflightMock.mockReset()
    deleteMutate.mockReset()
  })

  it('keeps the delete button disabled until the typed name matches exactly', () => {
    setPreflight(makePreflight())
    render(<DeleteModelDialog engine={ENGINE} onClose={() => {}} />)

    const btn = screen.getByRole('button', { name: /永久删除/ })
    expect(btn).toBeDisabled()

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Doomed-Mode' } })
    expect(btn).toBeDisabled()

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Doomed-Model' } })
    expect(btn).toBeEnabled()
  })

  it('renders no delete button at all when the model is still in VRAM', () => {
    setPreflight(makePreflight({ blockers: { loaded: { status: 'loaded', gpu: 1 }, services: [] } }))
    render(<DeleteModelDialog engine={ENGINE} onClose={() => {}} />)

    expect(screen.queryByRole('button', { name: /永久删除/ })).toBeNull()
    expect(screen.getByText(/请先卸载/)).toBeTruthy()
  })

  it('requires the acknowledge checkbox when services reference the model', () => {
    setPreflight(
      makePreflight({
        blockers: { loaded: null, services: [{ id: '1', name: 'doomed-api' }] },
      }),
    )
    render(<DeleteModelDialog engine={ENGINE} onClose={() => {}} />)

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Doomed-Model' } })
    const btn = screen.getByRole('button', { name: /永久删除/ })
    expect(btn).toBeDisabled()

    fireEvent.click(screen.getByRole('checkbox'))
    expect(btn).toBeEnabled()

    fireEvent.click(btn)
    expect(deleteMutate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'doomed_model', force: true }),
      expect.anything(),
    )
  })

  it('shows the copy-cleanup-prompt button only when code refs exist', () => {
    setPreflight(makePreflight())
    const { unmount } = render(<DeleteModelDialog engine={ENGINE} onClose={() => {}} />)
    expect(screen.queryByRole('button', { name: /复制清理 prompt/ })).toBeNull()
    unmount()

    setPreflight(
      makePreflight({
        code_refs: [{ file: 'src/api/routes/services.py', line: 377, text: 'doomed_model' }],
      }),
    )
    render(<DeleteModelDialog engine={ENGINE} onClose={() => {}} />)
    expect(screen.getByRole('button', { name: /复制清理 prompt/ })).toBeTruthy()
  })

  it('falls back to a selectable textarea when both clipboard paths fail', async () => {
    // 生产经明文 HTTP 内网访问 → 非安全上下文,navigator.clipboard 确实会缺失;
    // copyText 的 execCommand 回退再失败,才展开 textarea 让用户手动复制。
    const original = navigator.clipboard
    const originalExec = Object.getOwnPropertyDescriptor(document, 'execCommand')
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    Object.defineProperty(document, 'execCommand', { value: () => false, configurable: true })
    try {
      setPreflight(
        makePreflight({
          code_refs: [{ file: 'a.py', line: 1, text: 'doomed_model' }],
        }),
      )
      render(<DeleteModelDialog engine={ENGINE} onClose={() => {}} />)

      fireEvent.click(screen.getByRole('button', { name: /复制清理 prompt/ }))

      const ta = (await screen.findByLabelText('清理 prompt')) as HTMLTextAreaElement
      expect(ta.value).toContain('doomed_model')
      expect(ta.value).toContain('a.py:1')
    } finally {
      Object.defineProperty(navigator, 'clipboard', { value: original, configurable: true })
      if (originalExec) Object.defineProperty(document, 'execCommand', originalExec)
      else delete (document as unknown as Record<string, unknown>).execCommand
    }
  })
})
