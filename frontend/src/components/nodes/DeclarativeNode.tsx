import { useState, useEffect, useRef, useCallback } from 'react'
import { NodeResizer, type NodeProps } from '@xyflow/react'
import { Zap, Check, ImageIcon } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useWorkspaceStore } from '../../stores/workspace'
import { useLightboxStore } from '../../stores/lightbox'
import { NODE_DEFS, type NodeType } from '../../models/workflow'
import { DECLARATIVE_NODES, type WidgetDef } from '../../models/nodeRegistry'
import { useAgents } from '../../api/agents'
import { apiFetch } from '../../api/client'
import { useEnginesLiveSync, type EngineInfo } from '../../api/engines'
import BaseNode, { NodeWidgetRow, NodeInput, NodeNumberDrag, NodeTextarea } from './BaseNode'
import NodeSelectPopover from './NodeSelectPopover'
import { readImageDropUrl, isDisplayableImageValue } from './imageDragDrop'

function AgentSelectWidget({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  const { data: agents } = useAgents()
  const opts = (agents ?? []).map((a) => ({ value: a.name, label: a.display_name || a.name }))
  return (
    <NodeSelectPopover
      value={value}
      onChange={onChange}
      options={opts}
      placeholder="选择 Agent..."
      size="compact"
    />
  )
}

function ModelSelectWidget({
  value,
  onChange,
  filter,
}: {
  value: string
  onChange: (v: string) => void
  filter?: string
}) {
  // Subscribe to /ws/models so the dropdown stays current as models load /
  // unload, even when no other component on the page mounts useEngines().
  useEnginesLiveSync()
  const params = filter ? `?type=${filter}` : ''
  const { data: engines } = useQuery({
    queryKey: ['engines', filter],
    queryFn: () => apiFetch<EngineInfo[]>(`/api/v1/engines${params}`),
  })

  const loaded = (engines ?? []).filter((e) => e.status === 'loaded')
  const unloaded = (engines ?? []).filter((e) => e.status !== 'loaded' && e.local_exists)

  const opts = [
    ...loaded.map((e) => ({
      value: e.name,
      label: e.display_name,
      description: '已加载',
      loaded: true,  // → 绿点 + 「只看已加载」筛选
    })),
    // 未加载:置灰不可选(同旧原生 select 的 disabled 语义)。
    ...unloaded.map((e) => ({
      value: e.name,
      label: e.display_name,
      description: '未加载',
      color: 'var(--muted)',
      disabled: true,
      loaded: false,
    })),
  ]
  return (
    <NodeSelectPopover
      value={value}
      onChange={onChange}
      options={opts}
      placeholder="选择模型..."
      size="compact"
    />
  )
}

function resolveValue(value: unknown, widget: WidgetDef): unknown {
  if (value !== undefined && value !== null) return value
  return widget.default
}

function WidgetRenderer({
  widget,
  value,
  onChange,
}: {
  widget: WidgetDef
  value: unknown
  onChange: (v: unknown) => void
}) {
  const resolved = resolveValue(value, widget)

  switch (widget.widget) {
    case 'input':
      return (
        <NodeInput
          value={String(resolved ?? '')}
          onChange={(e) => onChange(e.target.value)}
          placeholder={widget.label}
        />
      )
    case 'textarea':
      return (
        <NodeTextarea
          value={String(resolved ?? '')}
          onChange={(e) => onChange(e.target.value)}
          style={widget.rows ? { height: widget.rows * 16 } : undefined}
        />
      )
    case 'select': {
      // options 兼容字符串列表(node.yaml 常写 [default, bfloat16])与对象列表(支持 description/color)。
      const opts = (widget.options ?? []).map((o) =>
        typeof o === 'string' ? { value: o, label: o } : o,
      )
      return (
        <NodeSelectPopover
          value={String(resolved ?? '')}
          onChange={(v) => onChange(v)}
          options={opts}
          size="compact"
        />
      )
    }
    case 'slider':
      return (
        <NodeNumberDrag
          value={Number(resolved ?? widget.min ?? 0)}
          onChange={onChange}
          min={widget.min}
          max={widget.max}
          step={widget.step}
          precision={widget.precision}
        />
      )
    case 'checkbox':
      return (
        <div
          onClick={() => onChange(!resolved)}
          className="nodrag"
          style={{
            width: 32, height: 16, borderRadius: 8, cursor: 'pointer',
            background: resolved ? 'var(--accent)' : 'var(--bg)',
            border: '1px solid var(--border)',
            position: 'relative', transition: 'background 0.2s',
          }}
        >
          <div style={{
            width: 12, height: 12, borderRadius: 6,
            background: '#fff', position: 'absolute', top: 1,
            left: resolved ? 17 : 1, transition: 'left 0.2s',
          }} />
        </div>
      )
    case 'agent_select':
      return (
        <AgentSelectWidget
          value={String(resolved ?? '')}
          onChange={(v) => onChange(v)}
        />
      )
    case 'model_select':
      return (
        <ModelSelectWidget
          value={String(resolved ?? '')}
          onChange={(v) => onChange(v)}
          filter={widget.filter}
        />
      )
    case 'image_upload':
      return (
        <ImageUploadWidget
          value={String(resolved ?? '')}
          onChange={(v) => onChange(v)}
        />
      )
    default:
      return null
  }
}

/** 图像上传 widget:选/拖/粘贴图 → base64 data URI 存进 node.data。喂 image→image 节点
 *  (SeedVR2 超分等)。有图显示缩略图 + 重传;无图显示上传框。 */
function ImageUploadWidget({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const openLightbox = useLightboxStore((s) => s.openFromUrl)
  const readFile = (file: File) => {
    if (!file.type.startsWith('image/')) return
    const reader = new FileReader()
    reader.onload = (e) => onChange((e.target?.result as string) || '')
    reader.readAsDataURL(file)
  }
  // 接受 base64 data URI、本站签名 URL(/files/...)或绝对 http(s) URL。
  // URL 形态来自「出图拖到输入」:画廊缩略图拖进来 / 「转为输入」生成的节点。
  const hasImage = isDisplayableImageValue(value)
  return (
    <div className="nodrag" style={{ width: '100%' }}>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        style={{ display: 'none' }}
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) readFile(f)
        }}
      />
      <div
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault()
          // 优先吃拖进来的图片 URL(画廊缩略图 → 输入)。后端 image_input PR-1 已接受本站 URL。
          const url = readImageDropUrl(e.dataTransfer)
          if (url) {
            onChange(url)
            return
          }
          const f = e.dataTransfer.files?.[0]
          if (f) readFile(f)
        }}
        style={{
          width: '100%', minHeight: hasImage ? undefined : 64,
          border: '1px dashed var(--border)', borderRadius: 6, cursor: 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          padding: 6, background: 'var(--bg)', overflow: 'hidden',
        }}
      >
        {hasImage ? (
          <img
            src={value}
            alt="upload"
            title="双击放大预览"
            onDoubleClick={(e) => { e.stopPropagation(); openLightbox(value) }}
            style={{ maxWidth: '100%', maxHeight: 140, borderRadius: 4, display: 'block', cursor: 'zoom-in' }}
          />
        ) : (
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>点击或拖拽上传图片</span>
        )}
      </div>
    </div>
  )
}

export default function DeclarativeNode({ id, type, data, selected }: NodeProps) {
  const updateNode = useWorkspaceStore((s) => s.updateNode)
  const nodeType = type as NodeType
  const declDef = DECLARATIVE_NODES[nodeType]
  const portDef = NODE_DEFS[nodeType]

  // Token stats state
  const [tokenStats, setTokenStats] = useState<{
    phase: 'streaming' | 'done'
    outputTokens: number
    inputTokens: number
    totalTokens: number
    tokensPerSec: number
    durationSec: number
  } | null>(null)
  const tokenCountRef = useRef(0)
  const firstTokenAtRef = useRef<number | null>(null)
  const throttleRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // PR-12:**删了旧的 imageStage 3 阶段假进度模拟器**(text_encode 1s →
  // denoise N×1s → vae_decode 0.5s)。它原本挂在 flux2_vae_decode 节点上,
  // 但 VAE Decode 在工作流末尾才执行,前面 Load Diffusion Model / KSampler
  // 跑的时候 VAE 节点根本没 node_start,反过来 VAE 真 node_start 触发后
  // simulation 从「Text encode...」开始,跟实际状态完全不同步 — 用户报告
  // 「正在加载模型,焦点跑到 VAE 那里显示 Denoise」就是这个错配。
  //
  // 现在 backend 已经按真节点 node_id 发 node_progress(KSampler 的 step
  // 事件挂在 KSampler 上),不需要 fake 模拟。每个节点的 denoiseProgress
  // 独立 state,谁收到自己的 step 谁渲染。
  //
  // 节点完成耗时只在「真的跑完」时显示(node_complete);loading / running
  // 的视觉由 BaseNode 通用 status chip(已存在)负责。
  const [doneElapsedSec, setDoneElapsedSec] = useState<number | null>(null)
  // node_complete.cached=true → 该节点结果来自缓存(L1/L2 组件缓存、留噪 latent 等),
  // 没真算 → 完成文案标「(cached)」,跟真跑完区分(见 project_component_l1_cache)。
  const [cachedDone, setCachedDone] = useState(false)
  const nodeStartAtRef = useRef<number | null>(null)
  // PR-3:真采样进度(每步从 backend 经 WS node_progress 事件来)。runner 的
  // P.NodeProgress 用 progress(0-1)+ detail("step N/T")—— parse detail 拿 step/total。
  const [denoiseProgress, setDenoiseProgress] = useState<
    { step: number; total: number; percent: number } | null
  >(null)
  // PR-F:latent 实时 RGB 预览(WS node_progress.preview_url,~96px JPEG data URI)。
  // 「看图慢慢长出来」—— ComfyUI 杀手锏的等价实现。node_complete 清。
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)

  const updateStreamingStats = useCallback(() => {
    const count = tokenCountRef.current
    const first = firstTokenAtRef.current
    if (count < 2 || !first) return
    const elapsed = (performance.now() - first) / 1000
    const rate = elapsed > 0 ? (count - 1) / elapsed : 0
    setTokenStats({
      phase: 'streaming',
      outputTokens: count,
      inputTokens: 0,
      totalTokens: 0,
      tokensPerSec: Math.round(rate * 10) / 10,
      durationSec: Math.round(elapsed * 10) / 10,
    })
  }, [])

  useEffect(() => {
    const handler = (event: CustomEvent) => {
      const data = event.detail
      if (data.type === 'node_stream' && data.node_id === id) {
        tokenCountRef.current++
        if (!firstTokenAtRef.current && tokenCountRef.current === 1) {
          firstTokenAtRef.current = performance.now()
        }
        if (!throttleRef.current) {
          throttleRef.current = setTimeout(() => {
            throttleRef.current = null
            updateStreamingStats()
          }, 250)
        }
      }
      if (data.type === 'node_progress' && data.node_id === id) {
        // 真采样进度:detail = "<stage> N/T"(后端 progress_tracker 发的是
        // "dit_denoise 1/25" / "text_encode 0/25" 等,stage 名前缀随引擎变;早先正则
        // 写死 "step N/T" → 对不上 dit_denoise → 进度条永不显示「RUNNING 无运行进度」)。
        // 放宽:抓任意 "N/T"(可有前缀词),不依赖具体 stage 字面词。progress 字段(0-1)
        // 仍优先用于百分比。
        const m = typeof data.detail === 'string' ? /(\d+)\s*\/\s*(\d+)/.exec(data.detail) : null
        if (m) {
          const step = Number(m[1])
          const total = Number(m[2])
          setDenoiseProgress({
            step,
            total,
            percent: typeof data.progress === 'number' ? Math.round(data.progress * 100) : Math.round((step / total) * 100),
          })
        }
        // PR-F:latent live preview thumbnail(若 backend 发了)。
        if (typeof data.preview_url === 'string' && data.preview_url) {
          setPreviewUrl(data.preview_url)
        }
      }
      if (data.type === 'node_start' && data.node_id === id) {
        // New run on this node — clear previous run's stats
        tokenCountRef.current = 0
        firstTokenAtRef.current = null
        setTokenStats(null)
        setDenoiseProgress(null)
        setPreviewUrl(null)
        setDoneElapsedSec(null)
        setCachedDone(false)
        nodeStartAtRef.current = performance.now()
      }
      if (data.type === 'node_complete' && data.node_id === id) {
        setDenoiseProgress(null)
        setPreviewUrl(null)
        if (throttleRef.current) {
          clearTimeout(throttleRef.current)
          throttleRef.current = null
        }
        const start = nodeStartAtRef.current
        const realElapsed = data.duration_ms
          ? data.duration_ms / 1000
          : start
            ? (performance.now() - start) / 1000
            : 0
        setDoneElapsedSec(realElapsed)
        setCachedDone(!!data.cached)
        nodeStartAtRef.current = null
        const usage = data.usage
        const durationMs = data.duration_ms
        const first = firstTokenAtRef.current
        const elapsed = durationMs
          ? durationMs / 1000
          : first
            ? (performance.now() - first) / 1000
            : 0
        const outTok = usage?.completion_tokens ?? usage?.output_tokens ?? tokenCountRef.current
        const inTok = usage?.prompt_tokens ?? usage?.input_tokens ?? 0
        const total = usage?.total_tokens ?? inTok + outTok
        const rate = elapsed > 0 ? outTok / elapsed : 0
        setTokenStats({
          phase: 'done',
          outputTokens: outTok,
          inputTokens: inTok,
          totalTokens: total,
          tokensPerSec: Math.round(rate * 10) / 10,
          durationSec: Math.round(elapsed * 10) / 10,
        })
        tokenCountRef.current = 0
        firstTokenAtRef.current = null
        // Keep the final stats visible until the next run of this node
        // triggers node_start; no auto-hide timer.
      }
    }
    window.addEventListener('node-progress', handler as any)
    return () => {
      window.removeEventListener('node-progress', handler as any)
      if (throttleRef.current) clearTimeout(throttleRef.current)
    }
  }, [id, nodeType, data.steps, updateStreamingStats])

  if (!declDef || !portDef) return null

  const handleResizeEnd = () => window.dispatchEvent(new Event('node-resize-end'))

  return (
    <>
    <NodeResizer
      isVisible={selected}
      minWidth={220}
      minHeight={80}
      onResizeEnd={handleResizeEnd}
      lineStyle={{ border: 'none' }}
      handleStyle={{ width: 12, height: 12, background: 'transparent', border: 'none' }}
    />
    <BaseNode
      title={declDef.label}
      badge={{
        label: declDef.badge,
        bg: `color-mix(in srgb, ${declDef.badgeColor} 15%, transparent)`,
        color: declDef.badgeColor,
      }}
      selected={selected}
      inputs={portDef.inputs}
      outputs={portDef.outputs}
    >
      {declDef.widgets.map((w) => (
        <NodeWidgetRow key={w.name} label={w.label} stretch={w.widget === 'textarea'}>
          <WidgetRenderer
            widget={w}
            value={data[w.name] as unknown}
            onChange={(v) => updateNode(id, { [w.name]: v })}
          />
        </NodeWidgetRow>
      ))}
      {/* Streaming text intentionally rendered ONLY in the downstream
          TextOutput node (data flows along edges). LLM node keeps only
          token stats below. */}
      {tokenStats && (
        <div
          className="flex items-center gap-1.5"
          style={{
            fontSize: 9,
            color: 'var(--muted)',
            padding: '4px 10px 6px',
            transition: 'opacity 0.5s',
            opacity: tokenStats.phase === 'done' ? 0.7 : 1,
          }}
        >
          {tokenStats.phase === 'streaming' ? (
            <Zap size={10} style={{ color: 'var(--warn)', flexShrink: 0 }} />
          ) : (
            <Check size={10} style={{ color: 'var(--ok)', flexShrink: 0 }} />
          )}
          {tokenStats.phase === 'streaming' ? (
            <span>
              生成中 · {tokenStats.tokensPerSec} tok/s · 输出 {tokenStats.outputTokens}
            </span>
          ) : (
            <span>
              输入 {tokenStats.inputTokens} · 输出 {tokenStats.outputTokens} · 合计 {tokenStats.totalTokens} · {tokenStats.tokensPerSec} tok/s · {tokenStats.durationSec}s
            </span>
          )}
        </div>
      )}
      {/* PR-F:latent live preview thumbnail(出图过程中节点上叠 96px JPEG,「看图慢慢长出来」)。 */}
      {previewUrl && (
        <div style={{ padding: '4px 10px 0', display: 'flex', justifyContent: 'center' }}>
          <img
            src={previewUrl}
            alt="latent preview"
            style={{
              maxWidth: '100%', maxHeight: 96, borderRadius: 4,
              border: '1px solid var(--border)',
              imageRendering: 'pixelated',
              opacity: 0.95,
            }}
          />
        </div>
      )}
      {/* PR-12:节点级实时进度 + 完成耗时 — **挂在真正跑的那个节点上**,不再
        统一由 VAE Decode 模拟。
        · denoiseProgress 来自 backend 按 node_id 的 node_progress(KSampler 自己发)
        · doneElapsedSec 是任何节点的 node_complete 真实 duration_ms
        · 两者都没有就什么都不渲染,节点保持 BaseNode 默认状态(loading chip 已经
          在 BaseNode 那边显示) */}
      {(denoiseProgress || doneElapsedSec != null) && (
        <div style={{ padding: '4px 10px 6px' }}>
          <div
            className="flex items-center gap-1.5"
            style={{
              fontSize: 9,
              color: 'var(--muted)',
              transition: 'opacity 0.5s',
              opacity: doneElapsedSec != null && !denoiseProgress ? 0.7 : 1,
            }}
          >
            {doneElapsedSec != null && !denoiseProgress ? (
              <Check size={10} style={{ color: 'var(--ok)', flexShrink: 0 }} />
            ) : (
              <ImageIcon size={10} style={{ color: 'var(--info)', flexShrink: 0 }} />
            )}
            <span>
              {denoiseProgress
                ? `step ${denoiseProgress.step}/${denoiseProgress.total} · ${denoiseProgress.percent}%`
                : `完成 · ${Math.round((doneElapsedSec ?? 0) * 10) / 10}s${cachedDone ? ' (cached)' : ''}`}
            </span>
          </div>
          {denoiseProgress && (
            <div style={{
              marginTop: 3, height: 2, background: 'var(--border)', borderRadius: 1, overflow: 'hidden',
            }}>
              <div style={{
                width: `${denoiseProgress.percent}%`, height: '100%',
                background: 'var(--accent)', transition: 'width 0.2s linear',
              }} />
            </div>
          )}
        </div>
      )}
    </BaseNode>
    </>
  )
}
