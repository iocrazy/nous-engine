import { useState, useEffect, useMemo } from 'react'
import { Copy, Check } from 'lucide-react'
import { NodeResizer, NodeResizeControl, type NodeProps } from '@xyflow/react'
import { NODE_DEFS } from '../../models/workflow'
import { useWorkspaceStore } from '../../stores/workspace'
import BaseNode from './BaseNode'
import { copyTextOrToast } from '../../utils/clipboard'

export default function TextOutputNode({ id, data, selected }: NodeProps) {
  const def = NODE_DEFS.text_output
  const [streamText, setStreamText] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [copied, setCopied] = useState(false)

  // Find the upstream node that feeds this TextOutput's "text" input.
  // Streaming tokens are emitted with that upstream node's id (e.g. LLM),
  // not ours — we need to listen for the upstream id.
  const tabs = useWorkspaceStore((s) => s.tabs)
  const activeTabId = useWorkspaceStore((s) => s.activeTabId)
  const upstreamNodeId = useMemo(() => {
    const wf = tabs.find((t) => t.id === activeTabId)?.workflow
    const edge = wf?.edges.find((e) => e.target === id)
    return edge?.source ?? null
  }, [tabs, activeTabId, id])

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail
      const streamSource = upstreamNodeId
      if (!streamSource) return
      if (detail.type === 'node_start' && detail.node_id === streamSource) {
        setStreamText('')
        setIsStreaming(true)
      }
      if (detail.type === 'node_stream' && detail.node_id === streamSource) {
        // Wave 1 renamed the chunk field from `token` → `content`
        // (workflow_executor dispatch layer). Also tolerate `delta` / `text`
        // used by a few plugin executors. Defensively coerce away `undefined`
        // so we never paint the literal string "undefined" into the buffer.
        const raw = detail.content ?? detail.token ?? detail.delta ?? detail.text
        const chunk = typeof raw === 'string' ? raw : ''
        if (!chunk && detail.content === undefined && detail.token === undefined) {
          // One-time diagnostic: the backend event shape drifted again.
          // Look at this in devtools to find the new field name.
          // eslint-disable-next-line no-console
          console.warn('[TextOutputNode] node_stream with no known chunk field:', detail)
        }
        if (chunk) setStreamText((prev) => prev + chunk)
      }
      if (detail.type === 'node_complete' && detail.node_id === streamSource) {
        setIsStreaming(false)
      }
    }
    window.addEventListener('node-progress', handler)
    return () => window.removeEventListener('node-progress', handler)
  }, [upstreamNodeId])

  // While streaming is active, prefer the rolling streamText so the output
  // node updates in real time. Once streaming ends, fall back to data.text
  // (which the executor updates with the final stripped result).
  const text = isStreaming
    ? streamText
    : ((data.text as string) || streamText || '')

  return (
    <>
    <NodeResizer
      isVisible={selected}
      minWidth={220}
      minHeight={80}
      onResizeEnd={() => window.dispatchEvent(new Event('node-resize-end'))}
      lineStyle={{ borderColor: 'var(--accent)', borderWidth: 1 }}
      handleStyle={{
        width: 8,
        height: 8,
        background: 'var(--accent)',
        border: '2px solid var(--card)',
        borderRadius: 2,
      }}
    />
    <BaseNode
      title={def.label}
      badge={{ label: 'IO', bg: 'rgba(59,130,246,0.15)', color: 'var(--info)' }}
      selected={selected}
      inputs={def.inputs}
      outputs={def.outputs}
    >
      <div style={{ padding: '4px 10px', minHeight: 40, flex: 1, display: 'flex', flexDirection: 'column', position: 'relative' }}>
        {text ? (
          <div style={{ position: 'relative', flex: 1, display: 'flex', flexDirection: 'column' }}>
            <button
              className="nodrag"
              onClick={() => {
                void copyTextOrToast(text).then((ok) => {
                  if (!ok) return
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1500)
                })
              }}
              style={{
                position: 'absolute',
                top: 4,
                right: 4,
                background: 'var(--bg-hover)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                padding: '3px 5px',
                cursor: 'pointer',
                color: copied ? 'var(--ok)' : 'var(--muted)',
                display: 'flex',
                alignItems: 'center',
                gap: 3,
                fontSize: 9,
                zIndex: 1,
                opacity: 0.7,
                transition: 'opacity 0.15s',
              }}
              onMouseOver={(e) => { (e.currentTarget as HTMLButtonElement).style.opacity = '1' }}
              onMouseOut={(e) => { (e.currentTarget as HTMLButtonElement).style.opacity = '0.7' }}
            >
              {copied ? <Check size={10} /> : <Copy size={10} />}
              {copied ? '已复制' : '复制'}
            </button>
            <div style={{
              padding: '8px',
              background: 'var(--bg)',
              borderRadius: 4,
              fontSize: 12,
              lineHeight: 1.5,
              flex: 1,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              color: 'var(--text)',
              border: '1px solid var(--border)',
            }}>
              {text}
              {streamText && <span style={{ opacity: 0.5 }}>▍</span>}
            </div>
          </div>
        ) : (
          <div style={{
            flex: 1,
            minHeight: 32,
            background: 'var(--bg)',
            borderRadius: 4,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 10,
            color: 'var(--muted-strong)',
          }}>
            等待文本输入...
          </div>
        )}
        {/* Always-visible grip pinned to the CARD's own bottom-right corner.
            Absolute-positioned inside this relative wrapper (which sits
            inside BaseNode), so its position follows the visible card edge
            regardless of the outer React Flow node wrapper size. */}
        <NodeResizeControl
          position="bottom-right"
          minWidth={220}
          minHeight={80}
          style={{
            position: 'absolute',
            right: 0,
            bottom: 0,
            background: 'transparent',
            border: 'none',
            width: 16,
            height: 16,
          }}
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 12 12"
            style={{
              position: 'absolute',
              right: 2,
              bottom: 2,
              pointerEvents: 'none',
              color: 'var(--muted-strong)',
            }}
          >
            <path
              d="M11 4 L4 11 M11 7 L7 11 M11 10 L10 11"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            />
          </svg>
        </NodeResizeControl>
      </div>
    </BaseNode>
    </>
  )
}
