import { useState } from 'react'
import { Copy, Check, Eye, EyeOff } from 'lucide-react'
import type { CatalogService } from '../../api/apiGateway'
import { copyTextOrToast } from '../../utils/clipboard'

const TABS = [
  { id: 'curl', label: 'curl' },
  { id: 'openai-python', label: 'openai (Python)' },
  { id: 'openai-node', label: 'openai (Node)' },
  { id: 'ollama', label: 'ollama CLI' },
] as const

type TabId = (typeof TABS)[number]['id']

interface Props {
  svc: CatalogService
}

export default function CodeSnippets({ svc }: Props) {
  const [tab, setTab] = useState<TabId>('curl')
  const [apiKey, setApiKey] = useState('sk-your-api-key-here')
  const [showKey, setShowKey] = useState(false)
  const [copied, setCopied] = useState(false)

  const maskedKey =
    apiKey.length > 12
      ? `${apiKey.slice(0, 6)}...${apiKey.slice(-4)}`
      : apiKey
  const displayKey = showKey ? apiKey : maskedKey
  const snippet = buildSnippet(tab, svc.instance_name, displayKey)

  const handleCopy = async () => {
    // Copy ALWAYS uses the real key, never the masked form.
    const raw = buildSnippet(tab, svc.instance_name, apiKey)
    if (!(await copyTextOrToast(raw))) return
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <div className="flex flex-col gap-2">
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '6px 10px',
          borderRadius: 4,
          border: '1px solid var(--border)',
          background: 'var(--bg)',
        }}
      >
        <span style={{ fontSize: 12, color: 'var(--muted)' }}>API Key</span>
        <input
          type={showKey ? 'text' : 'password'}
          placeholder="sk-..."
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          style={{
            flex: 1,
            background: 'transparent',
            outline: 'none',
            border: 'none',
            fontSize: 12,
            color: 'var(--text)',
            fontFamily: 'JetBrains Mono, monospace',
          }}
        />
        <button
          type="button"
          onClick={() => setShowKey((s) => !s)}
          style={{ color: 'var(--muted)', cursor: 'pointer' }}
          aria-label={showKey ? 'hide key' : 'show key'}
        >
          {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>

      <div
        className="flex items-center gap-1"
        style={{ borderBottom: '1px solid var(--border)' }}
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            style={{
              padding: '6px 10px',
              fontSize: 12,
              color: tab === t.id ? 'var(--text)' : 'var(--muted)',
              borderBottom: tab === t.id ? '2px solid var(--accent)' : '2px solid transparent',
              background: 'transparent',
              cursor: 'pointer',
            }}
          >
            {t.label}
          </button>
        ))}
        <div style={{ flex: 1 }} />
        <button
          type="button"
          onClick={handleCopy}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 4,
            padding: '4px 8px',
            fontSize: 12,
            color: 'var(--muted)',
            cursor: 'pointer',
          }}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>

      <pre
        style={{
          padding: 12,
          borderRadius: 4,
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          color: 'var(--text)',
          fontSize: 12,
          fontFamily: 'JetBrains Mono, monospace',
          overflow: 'auto',
          margin: 0,
          lineHeight: 1.6,
        }}
      >
        {snippet}
      </pre>
    </div>
  )
}

function buildSnippet(tab: TabId, model: string, key: string): string {
  switch (tab) {
    case 'curl':
      return [
        'curl http://localhost:8000/v1/chat/completions \\',
        `  -H "Authorization: Bearer ${key}" \\`,
        '  -H "Content-Type: application/json" \\',
        '  -d \'{',
        `    "model": "${model}",`,
        '    "messages": [{"role": "user", "content": "Hello"}],',
        '    "stream": false',
        '  }\'',
      ].join('\n')
    case 'openai-python':
      return [
        'from openai import OpenAI',
        '',
        'client = OpenAI(',
        `    api_key="${key}",`,
        '    base_url="http://localhost:8000/v1",',
        ')',
        '',
        'resp = client.chat.completions.create(',
        `    model="${model}",`,
        '    messages=[{"role": "user", "content": "Hello"}],',
        ')',
        'print(resp.choices[0].message.content)',
      ].join('\n')
    case 'openai-node':
      return [
        "import OpenAI from 'openai'",
        '',
        'const client = new OpenAI({',
        `  apiKey: '${key}',`,
        "  baseURL: 'http://localhost:8000/v1',",
        '})',
        '',
        'const resp = await client.chat.completions.create({',
        `  model: '${model}',`,
        "  messages: [{ role: 'user', content: 'Hello' }],",
        '})',
        'console.log(resp.choices[0].message.content)',
      ].join('\n')
    case 'ollama':
      return [
        '# Set OLLAMA_HOST to point at nous-center',
        'export OLLAMA_HOST=http://localhost:8000',
        '',
        '# The Ollama CLI does not support auth headers natively.',
        '# Use curl or the ollama-python SDK with a custom client:',
        '',
        'curl http://localhost:8000/api/chat \\',
        `  -H "Authorization: Bearer ${key}" \\`,
        '  -H "Content-Type: application/json" \\',
        '  -d \'{',
        `    "model": "${model}",`,
        '    "messages": [{"role": "user", "content": "Hello"}]',
        '  }\'',
      ].join('\n')
  }
}
