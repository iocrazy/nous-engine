import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const apiFetch = vi.fn()
vi.mock('../../api/client', () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }))

import { ModelPlayground } from './ModelPlayground'

// 花括号不能省:箭头函数若返回 mockReset() 的返回值(即 mock 本身),vitest 会把它当
// 清理钩子在用例结束后调用一次 —— 失败用例里那次调用会抛错,把本该通过的用例判红。
beforeEach(() => {
  apiFetch.mockReset()
})

describe('ModelPlayground · LLM', () => {
  it('发出带用户消息的 chat 请求(不是 messages: [])并显示回答与思考内容', async () => {
    apiFetch.mockResolvedValue({
      choices: [{ message: { content: '你好呀', reasoning_content: '先打个招呼' } }],
      usage: { prompt_tokens: 5, completion_tokens: 3 },
    })
    render(<ModelPlayground name="qwen3-8-27b-huihui" category="llm" />)
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '你好' } })
    fireEvent.click(screen.getByText('▶ 发送'))

    await waitFor(() => expect(screen.getByText('你好呀')).toBeTruthy())
    const [url, init] = apiFetch.mock.calls[0]
    expect(url).toBe('/v1/chat/completions')
    const body = JSON.parse((init as { body: string }).body)
    expect(body.model).toBe('qwen3-8-27b-huihui')
    expect(body.messages).toEqual([{ role: 'user', content: '你好' }])
    expect(screen.getByText('先打个招呼')).toBeTruthy()
  })

  it('多轮时只回传 content,不把 reasoning 带回给模型', async () => {
    apiFetch
      .mockResolvedValueOnce({ choices: [{ message: { content: 'A1', reasoning_content: 'R1' } }] })
      .mockResolvedValueOnce({ choices: [{ message: { content: 'A2' } }] })
    render(<ModelPlayground name="m" category="llm" />)
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: 'Q1' } })
    fireEvent.click(screen.getByText('▶ 发送'))
    await waitFor(() => expect(screen.getByText('A1')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: 'Q2' } })
    fireEvent.click(screen.getByText('▶ 发送'))
    await waitFor(() => expect(screen.getByText('A2')).toBeTruthy())

    const body = JSON.parse((apiFetch.mock.calls[1][1] as { body: string }).body)
    expect(body.messages).toEqual([
      { role: 'user', content: 'Q1' },
      { role: 'assistant', content: 'A1' },
      { role: 'user', content: 'Q2' },
    ])
  })

  it('失败时显示错误、消息退回输入框', async () => {
    apiFetch.mockImplementation(async () => { throw new Error('model_not_ready') })
    render(<ModelPlayground name="m" category="llm" />)
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByText('▶ 发送'))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('model_not_ready'))
    expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('hi')
  })
})

describe('ModelPlayground · embedding', () => {
  it('走 /v1/embeddings(不是 /v1/apps/…/run),WeMM 默认 messages 格式,显示维度', async () => {
    apiFetch.mockResolvedValue({ data: [{ embedding: [0.6, 0.8, 0, 0] }], usage: { prompt_tokens: 2 } })
    render(<ModelPlayground name="wemm-embedding-4b" category="embedding" />)
    fireEvent.change(screen.getByLabelText('待向量化文本'), { target: { value: '猫' } })
    fireEvent.click(screen.getByText('▶ 向量化'))
    await waitFor(() => expect(screen.getByText(/维度 4/)).toBeTruthy())
    const [url, init] = apiFetch.mock.calls[0]
    expect(url).toBe('/v1/embeddings')
    expect(JSON.parse((init as { body: string }).body)).toEqual({
      model: 'wemm-embedding-4b', messages: [{ role: 'user', content: '猫' }],
    })
    expect(screen.getByText(/范数 1.0000/)).toBeTruthy()
  })

  it('非 WeMM 默认走 input', async () => {
    apiFetch.mockResolvedValue({ data: [{ embedding: [1] }] })
    render(<ModelPlayground name="qwen3-embedding-4b" category="embedding" />)
    fireEvent.change(screen.getByLabelText('待向量化文本'), { target: { value: 'x' } })
    fireEvent.click(screen.getByText('▶ 向量化'))
    await waitFor(() => expect(apiFetch).toHaveBeenCalled())
    expect(JSON.parse((apiFetch.mock.calls[0][1] as { body: string }).body)).toEqual({
      model: 'qwen3-embedding-4b', input: 'x',
    })
  })
})
