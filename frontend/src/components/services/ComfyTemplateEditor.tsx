// ComfyUI 模板服务的「编辑」分支(Task 9,spec §「读图选节点配置」)。替代
// comfy_template 来源服务在 ServiceDetail 里的旧节点图编辑器:读原始 ComfyUI 工作流
// JSON,不依赖 nous 声明式节点表(那套只认自家节点类型)。流程:
//   1) sidecar 状态行(在线才能拿 object_info 类型预填,离线仍可手工配置)。
//   2) 重新上传替换 workflow_json(节点结构变了 → 旧映射按 comfy_node_id/comfy_input
//      定位失效,标红提示,不自动删,留给管理员确认)。
//   3) React Flow 通用节点卡(标题=class_type,与 nous 节点图共用视觉语言但节点内容
//      来自原始 class_type/inputs,不经 DECLARATIVE_NODES)。点卡开弹窗配置该节点的
//      原始 inputs(节点引用连线 [nodeId,slot] 不算可配置字段,过滤掉)。
//   4) 弹窗与下方字段汇总表共享同一份 exposedParams state,保存走 putMapping。
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Upload, X } from 'lucide-react'
import {
  getComfyHealth,
  getComfyTemplate,
  getObjectInfo,
  putMapping,
  reuploadComfyTemplate,
  type ComfyExposedParam,
  type ComfyHealth,
  type ComfyTemplateDetail,
} from '../../api/comfyTemplates'
import { paramKey, type ExposedParam } from '../../api/services'
import { Field } from '../playground/SchemaDrivenForm'
import { defaultFor } from '../playground/fieldKind'
import { isComfyNodeRef, layoutComfyGraph, type ComfyWorkflow } from './comfyGraphLayout'
import { placePopover } from './popoverPlacement'
import { findInvalidModelRefs, type ModelRefIssue } from './workflowModelCheck'
import ComfyTemplateGraph from './ComfyTemplateGraph'

/** 弹窗/汇总表共享的单行草稿。与后端 mapping 契约(comfy_node_id/comfy_input)同形 —
 *  这里就是 ComfyExposedParam 的别名,不额外分叉一套类型跟后端契约慢慢漂移。 */
export type ExposedParamDraft = ComfyExposedParam

export interface ComfyTemplateEditorProps {
  templateId: string | number
  /** 承载该模板的服务 id(有就传)——保存后用来 invalidate 该服务详情的 react-query
   *  缓存,否则「运行」分段的 Playground 表单还显示旧 exposed_inputs(见 services.ts
   *  useService 的 queryKey ['service', id])。 */
  serviceId?: string | number
}

interface RawInputRow {
  inputName: string
  rawValue: unknown
}

interface FieldMeta {
  type: string
  min?: number
  max?: number
  step?: number
  options?: unknown[]
}

const NUMERIC_TYPES = new Set(['integer', 'number'])
// 「未传时剪掉」开关只对桥**会上传**的类型出现:与后端 comfy/upload_inputs.UPLOAD_TYPES
// 同一集合(大小写一致,没有 binary —— 桥不上传 binary,标了也不会剪,后端会 400)。
const OMITTABLE_TYPES = new Set(['media', 'image', 'file', 'audio', 'video'])
// C2/I1 fix: 'image' 让这个字段在 Playground 渲染成文件选择器(值以 data URI 形式提交,
// 见 SchemaDrivenForm.tsx classifyField 认的 file|image|audio|video|binary 类型集合)。
// 后端 comfy_bridge.py 的上传触发条件同步放宽到接受这个词汇(见 _UPLOAD_TYPES)——挑
// "image" 而不是更宽泛的字面量 "media",是因为 classifyField 只认前者,选后者的话字段
// 会被误判成普通字符串输入,渲不出文件选择器。
const TYPE_OPTIONS: Array<{ v: string; label: string }> = [
  { v: 'string', label: '文本' },
  { v: 'integer', label: '整数' },
  { v: 'number', label: '小数' },
  { v: 'boolean', label: '开关' },
  { v: 'image', label: '图片/文件(上传)' },
]

function classInputDecl(
  classType: string,
  inputName: string,
  objectInfo: Record<string, unknown> | null,
): [unknown, Record<string, unknown>?] | null {
  const cls = objectInfo?.[classType] as
    | { input?: { required?: Record<string, unknown>; optional?: Record<string, unknown> } }
    | undefined
  const input = cls?.input
  const decl = input?.required?.[inputName] ?? input?.optional?.[inputName]
  return Array.isArray(decl) ? (decl as [unknown, Record<string, unknown>?]) : null
}

function numericConstraints(c?: Record<string, unknown>): Partial<FieldMeta> {
  if (!c) return {}
  const out: Partial<FieldMeta> = {}
  if (typeof c.min === 'number') out.min = c.min
  if (typeof c.max === 'number') out.max = c.max
  if (typeof c.step === 'number') out.step = c.step
  return out
}

/** object_info 有该 class 的该 input 声明 → 按它给的 TYPE/OPTIONS + min/max/step 走;
 *  否则(sidecar 离线、或该节点类型未在 object_info 出现)按工作流里的原始值类型兜底推断。 */
function deriveFieldMeta(
  classType: string,
  inputName: string,
  rawValue: unknown,
  objectInfo: Record<string, unknown> | null,
): FieldMeta {
  const decl = classInputDecl(classType, inputName, objectInfo)
  if (decl) {
    const [typeOrOptions, constraints] = decl
    if (Array.isArray(typeOrOptions)) {
      return { type: 'string', options: typeOrOptions, ...numericConstraints(constraints) }
    }
    const t = String(typeOrOptions).toUpperCase()
    const type = t === 'INT' ? 'integer' : t === 'FLOAT' ? 'number' : t === 'BOOLEAN' ? 'boolean' : 'string'
    return { type, ...numericConstraints(constraints) }
  }
  if (typeof rawValue === 'boolean') return { type: 'boolean' }
  if (typeof rawValue === 'number') return { type: Number.isInteger(rawValue) ? 'integer' : 'number' }
  return { type: 'string' }
}

function isSeedLike(inputName: string, type: string): boolean {
  return type === 'integer' && /seed/i.test(inputName)
}

/** 同 ImportComfyDialog 的导入格式校验(API 格式 = 每个顶层值都是带 class_type 的节点对象)。 */
function isApiFormatWorkflow(parsed: unknown): parsed is ComfyWorkflow {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false
  const values = Object.values(parsed as Record<string, unknown>)
  if (values.length === 0) return false
  return values.every(
    (v) => !!v && typeof v === 'object' && !Array.isArray(v) && 'class_type' in (v as object),
  )
}

export default function ComfyTemplateEditor({ templateId, serviceId }: ComfyTemplateEditorProps) {
  const idStr = String(templateId)
  const qc = useQueryClient()
  const [detail, setDetail] = useState<ComfyTemplateDetail | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [health, setHealth] = useState<ComfyHealth | null>(null)
  const [objectInfo, setObjectInfo] = useState<Record<string, unknown> | null>(null)
  const [exposedParams, setExposedParams] = useState<ExposedParamDraft[]>([])
  const [staleKeys, setStaleKeys] = useState<string[]>([])
  // 事故背景见 workflowModelCheck.ts 头注:导入的工作流可能带着别的机器冻结下来的模型
  // 文件名/路径,本机根本没有。初始加载和重新上传后都跑一遍,让「哪些节点卡引用了本机
  // 不存在的模型」在图上直接看得见,不用等真跑一次才在 ComfyUI 侧炸出 `Value not in list`。
  const [modelIssues, setModelIssues] = useState<ModelRefIssue[]>([])
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null)
  // 点击节点时记录的 xyflow 包裹 DOM(见 ComfyTemplateGraph 的 onNodeClick)——popover 定位
  // (placePopover)靠它的 getBoundingClientRect() 算贴靠位置,不用另外查 DOM。
  const [activeNodeEl, setActiveNodeEl] = useState<HTMLElement | null>(null)
  const graphContainerRef = useRef<HTMLDivElement>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setLoadError(null)
    setExposedParams([])
    setStaleKeys([])
    setModelIssues([])
    setActiveNodeId(null)
    setActiveNodeEl(null)
    Promise.all([
      getComfyTemplate(templateId),
      getComfyHealth().catch(() => null),
      getObjectInfo().catch(() => null),
    ])
      .then(([tpl, h, info]) => {
        if (cancelled) return
        setDetail(tpl)
        setExposedParams(tpl.exposed_params ?? [])
        setHealth(h)
        setObjectInfo(info)
        setModelIssues(findInvalidModelRefs(tpl.workflow_json as ComfyWorkflow, info))
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [templateId])

  const workflow = (detail?.workflow_json ?? {}) as ComfyWorkflow
  const idSet = useMemo(() => new Set(Object.keys(workflow)), [workflow])

  const layout = useMemo(() => layoutComfyGraph(workflow), [workflow])
  const usedCountByNode = useMemo(() => {
    const m = new Map<string, number>()
    for (const p of exposedParams) m.set(p.comfy_node_id, (m.get(p.comfy_node_id) ?? 0) + 1)
    return m
  }, [exposedParams])
  const modelIssueNodeIds = useMemo(() => new Set(modelIssues.map((i) => i.nodeId)), [modelIssues])
  const graphNodes = useMemo(
    () =>
      layout.nodes.map((n) => ({
        ...n,
        usedCount: usedCountByNode.get(n.id) ?? 0,
        invalidModel: modelIssueNodeIds.has(n.id),
      })),
    [layout.nodes, usedCountByNode, modelIssueNodeIds],
  )

  const rawRowsFor = (nodeId: string): RawInputRow[] => {
    const node = workflow[nodeId]
    if (!node) return []
    return Object.entries(node.inputs ?? {})
      .filter(([, v]) => !isComfyNodeRef(v, idSet))
      .map(([inputName, rawValue]) => ({ inputName, rawValue }))
  }

  const activeNode = activeNodeId ? workflow[activeNodeId] : null
  const activeRows = activeNodeId ? rawRowsFor(activeNodeId) : []
  const exposedByInput = useMemo(() => {
    const m = new Map<string, ExposedParamDraft>()
    if (!activeNodeId) return m
    for (const p of exposedParams) if (p.comfy_node_id === activeNodeId) m.set(p.comfy_input, p)
    return m
  }, [exposedParams, activeNodeId])

  const toggleExpose = (nodeId: string, inputName: string, rawValue: unknown) => {
    setExposedParams((prev) => {
      const existing = prev.find((p) => p.comfy_node_id === nodeId && p.comfy_input === inputName)
      if (existing) return prev.filter((p) => p !== existing)
      const classType = workflow[nodeId]?.class_type ?? ''
      const meta = deriveFieldMeta(classType, inputName, rawValue, objectInfo)
      const keysInUse = new Set(prev.map((p) => p.key))
      const key = keysInUse.has(inputName) ? `${inputName}_${nodeId}` : inputName
      const draft: ExposedParamDraft = {
        key,
        label: inputName,
        type: meta.type,
        comfy_node_id: nodeId,
        comfy_input: inputName,
        default: rawValue,
        required: true,
        ...(meta.min !== undefined ? { min: meta.min } : {}),
        ...(meta.max !== undefined ? { max: meta.max } : {}),
        ...(meta.step !== undefined ? { step: meta.step } : {}),
        ...(meta.options ? { options: meta.options } : {}),
      }
      return [...prev, draft]
    })
    setSaved(false)
  }

  const patchExposed = (nodeId: string, inputName: string, patch: Partial<ExposedParamDraft>) => {
    setExposedParams((prev) =>
      prev.map((p) => (p.comfy_node_id === nodeId && p.comfy_input === inputName ? { ...p, ...patch } : p)),
    )
    setSaved(false)
  }

  const handleNodeClick = (nodeId: string, nodeEl: HTMLElement) => {
    setActiveNodeId(nodeId)
    setActiveNodeEl(nodeEl)
  }

  const closePopover = () => {
    setActiveNodeId(null)
    setActiveNodeEl(null)
  }

  // Task 1(popover 定位):贴节点右侧/翻左/夹边界的算术全在 popoverPlacement.ts(纯函数,
  // 已单测)—— 这里只读一次 DOM 矩形喂给它。在渲染期间直接读 ref 而不是丢进 effect 再
  // setState,是因为这只是渲染要用的派生布局值、不回写状态,省一次多余的 re-render;
  // activeNodeEl 为空(理论上不会发生,兜底)时退化成贴容器左上角,不崩。
  const POP_W = 380
  let popLeft = 8
  let popTop = 8
  let popMaxHeight = 400
  if (activeNodeId && graphContainerRef.current) {
    const containerEl = graphContainerRef.current
    const containerRect = containerEl.getBoundingClientRect()
    const nodeRect = activeNodeEl?.getBoundingClientRect() ?? containerRect
    popMaxHeight = Math.min(containerEl.clientHeight - 40, window.innerHeight * 0.7)
    const pos = placePopover({
      nodeRect,
      containerRect,
      scrollLeft: containerEl.scrollLeft,
      scrollTop: containerEl.scrollTop,
      clientW: containerEl.clientWidth,
      clientH: containerEl.clientHeight,
      popW: POP_W,
      popH: popMaxHeight,
    })
    popLeft = pos.left
    popTop = pos.top
  }

  const save = async () => {
    setSaving(true)
    setSaved(false)
    setSaveError(null)
    try {
      await putMapping(idStr, exposedParams)
      setSaved(true)
      // 保存改的是对外 exposed_inputs schema — invalidate 服务详情缓存,否则「运行」
      // 分段的 Playground 表单还是切 tab 前的旧字段(services.ts useService/useServices)。
      qc.invalidateQueries({ queryKey: ['services'] })
      if (serviceId != null) qc.invalidateQueries({ queryKey: ['service', String(serviceId)] })
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const handleReuploadFile = (file: File) => {
    setUploadError(null)
    const reader = new FileReader()
    reader.onerror = () => setUploadError('读取文件失败')
    reader.onload = async () => {
      let parsed: unknown
      try {
        parsed = JSON.parse(String(reader.result))
      } catch {
        setUploadError('不是合法 JSON')
        return
      }
      if (!isApiFormatWorkflow(parsed)) {
        setUploadError('不是 ComfyUI API 格式导出，请在 ComfyUI 用 Export (API) 导出')
        return
      }
      try {
        const res = await reuploadComfyTemplate(idStr, parsed)
        setDetail((d) => (d ? { ...d, workflow_json: parsed } : d))
        setStaleKeys(res.stale_keys)
        setModelIssues(findInvalidModelRefs(parsed, objectInfo))
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : String(e))
      }
    }
    reader.readAsText(file)
  }

  if (loadError) {
    return <div style={{ fontSize: 12, color: 'var(--error, #ef4444)', padding: 14 }}>{loadError}</div>
  }
  if (!detail) {
    return <div style={{ fontSize: 12, color: 'var(--muted)', padding: 14 }}>加载中…</div>
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <SidecarStatusLine health={health} />
      <ReuploadZone onFile={handleReuploadFile} error={uploadError} staleKeys={staleKeys} />

      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 12,
          padding: '10px 14px', background: 'var(--bg-accent)',
          border: '1px solid var(--border)', borderRadius: 8,
        }}
      >
        <div style={{ flex: 1, fontSize: 11, color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
          <AlertTriangle size={12} style={{ color: 'var(--warn, #f59e0b)', flexShrink: 0 }} />
          点节点卡配置要暴露的字段 · 改动会更新对外 API schema
        </div>
        {saved && !saving && <span style={{ fontSize: 11, color: 'var(--ok, #34c759)' }}>已保存</span>}
        {saveError && <span style={{ fontSize: 11, color: 'var(--error, #ef4444)' }}>{saveError}</span>}
        <button type="button" onClick={save} disabled={saving} style={saveBtnStyle(saving)}>
          {saving ? '保存中…' : '保存配置'}
        </button>
      </div>

      <div style={{ display: 'flex', gap: 10, height: '58vh', minHeight: 420 }}>
        <div
          style={{
            width: 300, flexShrink: 0, display: 'flex', flexDirection: 'column',
            border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden',
            background: 'var(--bg-accent)',
          }}
        >
          <CanvasPreviewPanel exposedParams={exposedParams} />
        </div>

        <div
          ref={graphContainerRef}
          style={{ flex: 1, minWidth: 0, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden', position: 'relative' }}
        >
          <ComfyTemplateGraph
            nodes={graphNodes}
            edges={layout.edges}
            activeNodeId={activeNodeId}
            onNodeClick={handleNodeClick}
          />
          {activeNodeId && activeNode && (
            <NodeConfigPopover
              nodeId={activeNodeId}
              classType={activeNode.class_type}
              rows={activeRows}
              exposedByInput={exposedByInput}
              objectInfo={objectInfo}
              left={popLeft}
              top={popTop}
              maxHeight={popMaxHeight}
              onToggle={(inputName, rawValue) => toggleExpose(activeNodeId, inputName, rawValue)}
              onPatch={(inputName, patch) => patchExposed(activeNodeId, inputName, patch)}
              onClose={closePopover}
            />
          )}
        </div>
      </div>

    </div>
  )
}

// ---------- sidecar status ----------

function SidecarStatusLine({ health }: { health: ComfyHealth | null }) {
  const lineStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 8,
    padding: '8px 14px', background: 'var(--bg-accent)',
    border: '1px solid var(--border)', borderRadius: 8,
    fontSize: 12, color: 'var(--text)',
  }
  if (!health) {
    return (
      <div style={lineStyle}>
        <Dot color="var(--muted)" />
        检测 sidecar 状态…
      </div>
    )
  }
  if (!health.online) {
    return (
      <div style={lineStyle}>
        <Dot color="var(--error, #ef4444)" />
        sidecar 离线 — 配置仍可手工编辑,但 object_info 类型/min/max 预填不可用
      </div>
    )
  }
  return (
    <div style={lineStyle}>
      <Dot color="var(--ok, #34c759)" />
      sidecar 在线 · v{health.version || '?'} · 队列 {health.queue_depth}
    </div>
  )
}

function Dot({ color }: { color: string }) {
  return <span style={{ width: 7, height: 7, borderRadius: '50%', background: color, flexShrink: 0 }} />
}

// ---------- reupload zone ----------

function ReuploadZone({ onFile, error, staleKeys }: {
  onFile: (f: File) => void
  error: string | null
  /** 重新上传后 comfy_node_id/comfy_input 已不存在的字段 key —— 汇总表删掉后,
   *  这条可见性挪到这里,否则用户不知道哪些映射被新 JSON 打断了。 */
  staleKeys: string[]
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={dropZoneStyle}>
        <Upload size={14} style={{ color: 'var(--muted)', flexShrink: 0 }} />
        <span style={{ fontSize: 12, color: 'var(--text)' }}>
          重新上传工作流 JSON(替换节点结构;指向旧节点的字段会标红,需确认/移除)
        </span>
        <input
          data-testid="comfy-reupload-input"
          type="file"
          accept=".json,application/json"
          style={{ display: 'none' }}
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) onFile(f)
            e.target.value = ''
          }}
        />
      </label>
      {error && <div style={{ fontSize: 11, color: 'var(--error, #ef4444)' }}>{error}</div>}
      {staleKeys.length > 0 && (
        <div data-testid="stale-keys-warning" style={{ fontSize: 11, color: 'var(--error, #ef4444)' }}>
          {staleKeys.length} 个字段的节点映射已失效(新 JSON 里找不到对应节点/输入):
          {' '}{staleKeys.join('、')} —— 请在节点卡上重新勾选或取消暴露
        </div>
      )}
    </div>
  )
}

// ---------- node config popover ----------

function ValueBadge({ value }: { value: unknown }) {
  if (typeof value === 'string') {
    const v = value.length > 50 ? value.slice(0, 50) + '…' : value
    return <span style={{ color: 'var(--text)', fontWeight: 700 }}>"{v}"</span>
  }
  if (typeof value === 'number') {
    return <span style={{ color: '#0369a1', fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{value}</span>
  }
  if (typeof value === 'boolean') {
    return <span style={{ color: value ? '#15803d' : '#b45309', fontWeight: 800 }}>{value ? '✓ true' : '✗ false'}</span>
  }
  if (value == null) return <span style={{ color: 'var(--muted)' }}>—</span>
  return <span style={{ color: 'var(--muted)' }}>{String(value)}</span>
}

function NumField({
  label, value, disabled, onChange,
}: {
  label: string
  value: number | undefined
  disabled: boolean
  onChange: (v: number | undefined) => void
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 10, color: 'var(--muted)', flex: 1 }}>
      {label}
      <input
        type="number"
        value={value ?? ''}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
        style={{ ...smallInput, opacity: disabled ? 0.5 : 1 }}
      />
    </label>
  )
}

function NodeConfigPopover({
  nodeId, classType, rows, exposedByInput, objectInfo, left, top, maxHeight, onToggle, onPatch, onClose,
}: {
  nodeId: string
  classType: string
  rows: RawInputRow[]
  exposedByInput: Map<string, ExposedParamDraft>
  objectInfo: Record<string, unknown> | null
  /** 相对图容器的定位(popoverPlacement.placePopover 算好传入)。 */
  left: number
  top: number
  maxHeight: number
  onToggle: (inputName: string, rawValue: unknown) => void
  onPatch: (inputName: string, patch: Partial<ExposedParamDraft>) => void
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <>
      {/* 点击空白处关闭 —— 透明覆盖层,不再是全屏遮暗背景(对齐 IC:弹窗贴节点旁边,
          遮暗背景会把它和节点的空间关联感盖掉,读起来更「浮空」而不是「弹窗盖住了页面」)。 */}
      <div onClick={onClose} style={{ position: 'absolute', inset: 0, zIndex: 20 }} />
      <div
        role="dialog"
        onWheel={(e) => e.stopPropagation()}
        style={{
          position: 'absolute', left, top,
          width: 380, maxWidth: 'calc(100% - 16px)', maxHeight,
          display: 'flex', flexDirection: 'column', zIndex: 21,
          background: 'var(--card)', border: '1px solid var(--border-strong, var(--border))',
          borderRadius: 14, boxShadow: 'var(--shadow-lg, 0 12px 40px rgba(0,0,0,0.25))',
          overflow: 'hidden',
        }}
      >
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '12px 14px',
          borderBottom: '1px solid var(--border)', background: 'var(--card-hl)',
        }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 800, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {classType}
            </div>
            <div style={{ fontSize: 11, color: 'var(--muted)' }}>#{nodeId}</div>
          </div>
          <button
            type="button" title="关闭" onClick={onClose}
            style={{ display: 'inline-flex', padding: 4, background: 'transparent', border: 'none', color: 'var(--muted)', cursor: 'pointer', borderRadius: 6 }}
          >
            <X size={16} />
          </button>
        </div>

        <div style={{ overflow: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
          {rows.length === 0 && (
            <div style={{ color: 'var(--muted)', fontSize: 12, padding: '12px 4px' }}>该节点无可配置的原始字段</div>
          )}
          {rows.map((r) => {
            const cur = exposedByInput.get(r.inputName)
            const active = !!cur
            const meta = deriveFieldMeta(classType, r.inputName, r.rawValue, objectInfo)
            const type = cur?.type ?? meta.type
            const numeric = NUMERIC_TYPES.has(type)
            const seedLike = isSeedLike(r.inputName, type)
            const fileLike = OMITTABLE_TYPES.has(type)
            return (
              <div
                key={r.inputName}
                data-input-row
                style={{
                  border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
                  borderRadius: 10, padding: 10,
                  background: active ? 'var(--accent-subtle, rgba(99,102,241,0.06))' : 'transparent',
                  display: 'flex', flexDirection: 'column', gap: 8,
                }}
              >
                <label style={{ display: 'flex', alignItems: 'flex-start', gap: 9, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    aria-label={`暴露 ${r.inputName}`}
                    checked={active}
                    onChange={() => onToggle(r.inputName, r.rawValue)}
                    style={{ marginTop: 2, cursor: 'pointer' }}
                  />
                  <span style={{ minWidth: 0, flex: 1 }}>
                    <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--text)' }}>{r.inputName}</span>
                    <span style={{ display: 'block', fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>
                      原值: <ValueBadge value={r.rawValue} />
                    </span>
                  </span>
                </label>

                <div style={{ display: 'flex', gap: 8 }}>
                  <input
                    style={{ ...smallInput, flex: 1, minWidth: 0, opacity: active ? 1 : 0.5 }}
                    placeholder="显示名"
                    value={(cur?.label ?? r.inputName) || ''}
                    disabled={!active}
                    onChange={(e) => onPatch(r.inputName, { label: e.target.value })}
                  />
                  <select
                    style={{ ...smallInput, width: 84, flexShrink: 0, opacity: active ? 1 : 0.5 }}
                    value={type}
                    disabled={!active}
                    onChange={(e) => onPatch(r.inputName, { type: e.target.value })}
                  >
                    {TYPE_OPTIONS.map((o) => (
                      <option key={o.v} value={o.v}>{o.label}</option>
                    ))}
                  </select>
                </div>

                {numeric && (
                  <div style={{ display: 'flex', gap: 8 }}>
                    <NumField
                      label="min" value={cur?.min ?? meta.min} disabled={!active}
                      onChange={(v) => onPatch(r.inputName, { min: v })}
                    />
                    <NumField
                      label="max" value={cur?.max ?? meta.max} disabled={!active}
                      onChange={(v) => onPatch(r.inputName, { max: v })}
                    />
                    <NumField
                      label="step" value={cur?.step ?? meta.step} disabled={!active}
                      onChange={(v) => onPatch(r.inputName, { step: v })}
                    />
                  </div>
                )}

                {seedLike && (
                  <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text)', opacity: active ? 1 : 0.5 }}>
                    <input
                      type="checkbox"
                      aria-label={`随机 ${r.inputName}`}
                      checked={!!cur?.random}
                      disabled={!active}
                      onChange={(e) => onPatch(r.inputName, { random: e.target.checked })}
                    />
                    每次调用随机取值(不由调用方传入)
                  </label>
                )}

                {fileLike && (
                  <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text)', opacity: active ? 1 : 0.5 }}>
                    <input
                      type="checkbox"
                      aria-label={`未传时剪掉 ${r.inputName}`}
                      checked={!!cur?.omit_when_empty}
                      disabled={!active}
                      // 可省略 ⇒ 非必填 + 清掉 default(后端拒这两种组合):暴露时 default 是
                      // 节点里烤死的占位文件名,留着的话桥 data.get(key, default) 永远不算「未传」,
                      // 开关静默失效、占位图照喂。
                      onChange={(e) => onPatch(r.inputName, e.target.checked
                        ? { omit_when_empty: true, required: false, default: null }
                        : { omit_when_empty: false })}
                    />
                    未传时剪掉该支路(可选参考图:不传就不喂模板占位图)
                  </label>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </>
  )
}

// ---------- canvas preview panel (Task 3) ----------

/** ExposedParamDraft(= ComfyExposedParam,平铺 min/max/step/options/random/comfy_node_id/
 *  comfy_input)→ SchemaDrivenForm 的 ExposedParam 形状(nested constraints,node_id/
 *  input_name)。两边字段语义一一对应,不是巧合:两套 exposed-params 契约本就是同一个
 *  概念在不同落地点(Comfy 桥 vs 声明式节点)的表示。写一个薄适配器换形状,换来直接复用
 *  <Field> 渲染逻辑(seed 🎲、file dataURI 上传、slider min/max 等分支都不用抄一遍)。 */
function toSchemaExposedParam(p: ExposedParamDraft): ExposedParam {
  const constraints: Record<string, unknown> = {}
  if (p.min !== undefined) constraints.min = p.min
  if (p.max !== undefined) constraints.max = p.max
  if (p.step !== undefined) constraints.step = p.step
  if (p.options) constraints.enum = p.options
  if (p.random) constraints.random = true
  return {
    key: p.key,
    input_name: p.comfy_input,
    node_id: p.comfy_node_id,
    label: p.label,
    type: p.type,
    required: p.required,
    default: p.default,
    constraints,
  }
}

/** IC 参考实现里的「画布节点预览 · 实时」左栏:按当前 exposedParams 草稿(节点图弹窗里
 *  勾选/编辑的同一份 state,单一数据源)渲染出跟运行时 Playground 表单同款的控件,让管理员
 *  在编辑阶段就能看到「暴露出去的字段实际长什么样」。仅预览,不接运行 —— 运行走 Playground
 *  的「运行」分段,这里放个提示文案说明(需求明确排除:这版不加运行按钮)。 */
function CanvasPreviewPanel({ exposedParams }: { exposedParams: ExposedParamDraft[] }) {
  const schemaParams = useMemo(() => exposedParams.map(toSchemaExposedParam), [exposedParams])
  const [values, setValues] = useState<Record<string, unknown>>({})

  useEffect(() => {
    setValues((prev) => {
      const next: Record<string, unknown> = {}
      for (const p of schemaParams) {
        const k = paramKey(p)
        if (!k) continue
        next[k] = k in prev ? prev[k] : defaultFor(p)
      }
      return next
    })
  }, [schemaParams])

  return (
    <div data-testid="canvas-preview-panel" style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--text)' }}>画布节点预览 · 实时</div>
        <div style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 2 }}>
          仅预览,不可运行 · 运行请到 Playground「运行」分段
        </div>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '14px' }}>
        {schemaParams.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--muted)', textAlign: 'center', padding: '20px 4px' }}>
            暂无暴露字段 — 在右侧节点图点节点卡配置
          </div>
        ) : (
          schemaParams.map((p) => {
            const k = paramKey(p)
            return (
              <Field
                key={k ?? `${p.node_id}.${p.input_name}`}
                param={p}
                value={k ? values[k] : undefined}
                onChange={(v) => {
                  if (k) setValues((prev) => ({ ...prev, [k]: v }))
                }}
              />
            )
          })
        )}
      </div>
    </div>
  )
}


// ---------- shared bits ----------

const smallInput: React.CSSProperties = {
  fontSize: 12, padding: '6px 8px',
  background: 'var(--bg)', color: 'var(--text)',
  border: '1px solid var(--border)', borderRadius: 6,
}

const dropZoneStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%',
  background: 'var(--bg-accent)', color: 'var(--text)',
  border: '1px dashed var(--border)', borderRadius: 8,
  padding: '10px 12px', fontSize: 12, cursor: 'pointer',
}

function saveBtnStyle(saving: boolean): React.CSSProperties {
  return {
    fontSize: 12, padding: '6px 14px', background: 'var(--accent)', color: '#fff',
    border: 'none', borderRadius: 4, cursor: saving ? 'not-allowed' : 'pointer',
    opacity: saving ? 0.6 : 1,
  }
}
