import { useState } from 'react'
import { apiFetch } from '../../api/client'

/**
 * 模型类服务(source_type=model)的 Playground:LLM 对话 / 向量。
 *
 * 为什么不走通用 PlaygroundTab:模型类服务没有 `exposed_inputs`(那是工作流/应用的
 * 暴露字段),通用表单只会显示「该服务没有暴露入参」,点运行发出 `messages: []`;
 * 向量服务更是被发去 `/v1/apps/{name}/run` 这个根本不对的端点(2026-09-22 用户报
 * Playground「Field required」时一并查出)。
 *
 * 鉴权:这里不带 Authorization,靠 admin 登录 cookie —— 后端 chat/embeddings 已改成
 * 「Bearer 优先,否则 admin 会话」(`_auth_bearer_or_admin`),admin 会话按服务名直查、
 * 不走 grant/配额。
 */

type ChatMsg = { role: 'system' | 'user' | 'assistant'; content: string; reasoning?: string }

const box = {
  background: 'var(--bg-accent)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  padding: '14px 18px',
  display: 'flex',
  flexDirection: 'column' as const,
  gap: 10,
}
const textareaStyle = {
  width: '100%',
  background: 'var(--bg)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  padding: 10,
  fontSize: 13,
  fontFamily: 'inherit',
  resize: 'vertical' as const,
}
const btn = (disabled: boolean) => ({
  alignSelf: 'flex-start' as const,
  padding: '7px 16px',
  borderRadius: 6,
  background: disabled ? 'var(--muted)' : 'var(--accent)',
  color: '#fff',
  border: 'none',
  fontSize: 13,
  cursor: disabled ? 'not-allowed' : 'pointer',
})

function errText(e: unknown): string {
  return (e as Error)?.message ?? String(e)
}

export function ModelPlayground({ name, category }: { name: string; category: string }) {
  return category === 'embedding' ? <EmbeddingPlayground name={name} /> : <ChatPlayground name={name} />
}

function ChatPlayground({ name }: { name: string }) {
  const [system, setSystem] = useState('')
  const [input, setInput] = useState('')
  const [history, setHistory] = useState<ChatMsg[]>([])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<string | null>(null)

  const send = async () => {
    const text = input.trim()
    if (!text || running) return
    const turn: ChatMsg = { role: 'user', content: text }
    const next = [...history, turn]
    setHistory(next)
    setInput('')
    setRunning(true)
    setError(null)
    const t0 = performance.now()
    try {
      const messages = [
        ...(system.trim() ? [{ role: 'system', content: system.trim() }] : []),
        // 思考内容不回传给模型(OpenAI 约定:reasoning 只在响应里,多轮只带 content)
        ...next.map(({ role, content }) => ({ role, content })),
      ]
      const data = await apiFetch<{
        choices?: { message?: { content?: string | null; reasoning_content?: string | null } }[]
        usage?: { prompt_tokens?: number; completion_tokens?: number }
      }>('/v1/chat/completions', {
        method: 'POST',
        body: JSON.stringify({ model: name, messages }),
      })
      const msg = data.choices?.[0]?.message
      setHistory([
        ...next,
        {
          role: 'assistant',
          content: msg?.content ?? '',
          reasoning: msg?.reasoning_content ?? undefined,
        },
      ])
      const ms = Math.round(performance.now() - t0)
      const u = data.usage
      setMeta(
        `${ms} ms` +
          (u ? ` · 输入 ${u.prompt_tokens ?? 0} tokens · 输出 ${u.completion_tokens ?? 0} tokens` : ''),
      )
    } catch (e) {
      setError(errText(e))
      setHistory(history) // 失败不留半截轮次,用户消息退回输入框
      setInput(text)
    } finally {
      setRunning(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <section style={box}>
        <details>
          <summary style={{ fontSize: 12, color: 'var(--muted)', cursor: 'pointer' }}>
            系统提示词(可选)
          </summary>
          <textarea
            aria-label="系统提示词"
            rows={3}
            value={system}
            onChange={(e) => setSystem(e.target.value)}
            style={{ ...textareaStyle, marginTop: 8 }}
          />
        </details>

        {history.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {history.map((m, i) => (
              <div key={i} style={{ fontSize: 13, lineHeight: 1.6 }}>
                <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 2 }}>
                  {m.role === 'user' ? '你' : '模型'}
                </div>
                {m.reasoning && (
                  <details style={{ marginBottom: 4 }}>
                    <summary style={{ fontSize: 11, color: 'var(--muted)', cursor: 'pointer' }}>
                      思考过程(reasoning_content)
                    </summary>
                    <div style={{ whiteSpace: 'pre-wrap', color: 'var(--muted)', fontSize: 12 }}>
                      {m.reasoning}
                    </div>
                  </details>
                )}
                <div style={{ whiteSpace: 'pre-wrap', color: 'var(--text)' }}>{m.content}</div>
              </div>
            ))}
          </div>
        )}

        <textarea
          aria-label="消息"
          rows={3}
          placeholder="输入消息,Ctrl+Enter 发送"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void send()
          }}
          style={textareaStyle}
        />
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button type="button" onClick={() => void send()} disabled={running || !input.trim()}
            style={btn(running || !input.trim())}>
            {running ? '生成中…' : '▶ 发送'}
          </button>
          {history.length > 0 && (
            <button type="button" disabled={running}
              onClick={() => { setHistory([]); setMeta(null); setError(null) }}
              style={{ fontSize: 12, background: 'none', border: '1px solid var(--border)',
                borderRadius: 6, padding: '6px 12px', color: 'var(--muted)', cursor: 'pointer' }}>
              清空对话
            </button>
          )}
          {meta && <span style={{ fontSize: 11, color: 'var(--muted)' }}>{meta}</span>}
        </div>
        {error && (
          <div role="alert" style={{ color: 'var(--danger, #ef4444)', fontSize: 13, whiteSpace: 'pre-wrap' }}>
            {error}
          </div>
        )}
      </section>
    </div>
  )
}

function EmbeddingPlayground({ name }: { name: string }) {
  // WeMM 系列必须走 messages(chat template 末尾追加 <embedding> token);input 字符串是
  // 裸 tokenize,同一句话两条路余弦只有 0.91/0.95(CLAUDE.md「Embedding 模型」节)。
  // 默认按服务名猜,用户可切。
  const [mode, setMode] = useState<'messages' | 'input'>(
    name.toLowerCase().includes('wemm') ? 'messages' : 'input',
  )
  const [text, setText] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [out, setOut] = useState<{ dim: number; head: number[]; norm: number; tokens?: number; ms: number } | null>(null)

  const run = async () => {
    if (!text.trim() || running) return
    setRunning(true)
    setError(null)
    setOut(null)
    const t0 = performance.now()
    try {
      const body =
        mode === 'messages'
          ? { model: name, messages: [{ role: 'user', content: text }] }
          : { model: name, input: text }
      const data = await apiFetch<{ data?: { embedding?: number[] }[]; usage?: { prompt_tokens?: number } }>(
        '/v1/embeddings',
        { method: 'POST', body: JSON.stringify(body) },
      )
      const vec = data.data?.[0]?.embedding ?? []
      setOut({
        dim: vec.length,
        head: vec.slice(0, 8),
        norm: Math.sqrt(vec.reduce((s, v) => s + v * v, 0)),
        tokens: data.usage?.prompt_tokens,
        ms: Math.round(performance.now() - t0),
      })
    } catch (e) {
      setError(errText(e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <section style={box}>
      <div style={{ display: 'flex', gap: 12, fontSize: 12, color: 'var(--muted)' }}>
        请求格式:
        {(['messages', 'input'] as const).map((m) => (
          <label key={m} style={{ display: 'flex', gap: 4, alignItems: 'center', cursor: 'pointer' }}>
            <input type="radio" checked={mode === m} onChange={() => setMode(m)} />
            {m}
          </label>
        ))}
      </div>
      <textarea aria-label="待向量化文本" rows={3} value={text}
        onChange={(e) => setText(e.target.value)} style={textareaStyle} />
      <button type="button" onClick={() => void run()} disabled={running || !text.trim()}
        style={btn(running || !text.trim())}>
        {running ? '计算中…' : '▶ 向量化'}
      </button>
      {error && (
        <div role="alert" style={{ color: 'var(--danger, #ef4444)', fontSize: 13 }}>{error}</div>
      )}
      {out && (
        <div style={{ fontSize: 12, color: 'var(--text)', fontFamily: 'var(--mono, monospace)', lineHeight: 1.7 }}>
          <div>维度 {out.dim} · 范数 {out.norm.toFixed(4)} · {out.ms} ms{out.tokens != null ? ` · ${out.tokens} tokens` : ''}</div>
          <div style={{ color: 'var(--muted)' }}>
            [{out.head.map((v) => v.toFixed(4)).join(', ')}{out.dim > 8 ? ', …' : ''}]
          </div>
        </div>
      )}
    </section>
  )
}
