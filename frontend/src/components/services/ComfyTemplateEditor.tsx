import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import {
  getComfyHealth,
  getComfyTemplate,
  getObjectInfo,
  putMapping,
  type ComfyCanvasLayout,
  type ComfyExposedParam,
  type ComfyHealth,
  type ComfyTemplateDetail,
} from '../../api/comfyTemplates'
import { paramKey, type ExposedParam } from '../../api/services'
import { Field } from '../playground/SchemaDrivenForm'
import LoRAListField from '../playground/LoRAListField'
import { defaultFor, isFieldRequired, isFieldVisible, loraDirectory } from '../playground/fieldKind'
import {
  filterComfyBranch, groupComfyNodes, isComfyNodeRef, layoutComfyGraph,
  type ComfyNodePositions, type ComfyWorkflow,
} from './comfyGraphLayout'
import ComfyWorkflowUpdate from './ComfyWorkflowUpdate'
import { mappingDependents, POWER_LORA_LOADER, powerLoraRows } from './comfyMapping'
import qwenCanvas from './qwen21CanvasLayout.json'
import './ComfyTemplateEditor.css'
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
  currentMode?: string
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
  if (classType === POWER_LORA_LOADER && inputName === 'loras') return { type: 'lora_list' }
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

const EMPTY_WORKFLOW: ComfyWorkflow = {}
const QWEN_MODE_NODES: Record<string, string> = {
  moss_text: '404', text: '9', reference: '344', edit: '125', oldx: '534', character_card: '534',
  outpaint: '179', remove: '282', upscale: '387',
}

function loadPositions(id: string): ComfyNodePositions {
  try {
    const value = JSON.parse(localStorage.getItem(`nous:comfy:positions:${id}`) ?? '{}')
    return Object.fromEntries(Object.entries(value as ComfyNodePositions).filter(([, position]) =>
      typeof position?.classType === 'string' && Number.isFinite(position.x) && Number.isFinite(position.y)))
  } catch {
    return {}
  }
}

export default function ComfyTemplateEditor({ templateId, serviceId, currentMode: runMode }: ComfyTemplateEditorProps) {
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
  const [view, setView] = useState<'graph' | 'fields'>('graph')
  const [arrangement, setArrangement] = useState<'grouped' | 'compact'>('grouped')
  const [mode, setMode] = useState('')
  const [search, setSearch] = useState('')
  const [focus, setFocus] = useState<{ group?: number; node?: string }>()
  const [positions, setPositions] = useState<ComfyNodePositions>(() => loadPositions(idStr))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [updating, setUpdating] = useState(false)
  const [reviewing, setReviewing] = useState(false)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setLoadError(null)
    setExposedParams([])
    setStaleKeys([])
    setModelIssues([])
    setActiveNodeId(null)
    setMode('')
    setFocus(undefined)
    setReviewing(false)
    setUpdating(false)
    setPositions(loadPositions(String(templateId)))
    Promise.all([
      getComfyTemplate(templateId),
      getComfyHealth().catch(() => null),
      getObjectInfo().catch(() => null),
    ])
      .then(([tpl, h, info]) => {
        if (cancelled) return
        setDetail(tpl)
        setExposedParams(tpl.exposed_params ?? [])
        setStaleKeys(tpl.stale_keys ?? [])
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

  const workflow = (detail?.workflow_json as ComfyWorkflow | undefined) ?? EMPTY_WORKFLOW
  const idSet = useMemo(() => new Set(Object.keys(workflow)), [workflow])
  const nativeCanvas = detail?.canvas_layout ?? (detail?.service_name === 'nous-qwen21-all-in-one'
    ? qwenCanvas as ComfyCanvasLayout : null)
  const grouped = arrangement === 'grouped' && !!nativeCanvas
  const modeField = exposedParams.find(field => field.key === 'input_mode' && field.widget === 'radio')
  const availableModes = useMemo(() => (modeField?.options ?? []).flatMap(item => {
    if (typeof item === 'string') return [{ value: item, label: item }]
    if (!item || typeof item !== 'object' || !('value' in item) || typeof item.value !== 'string') return []
    return [{ value: item.value, label: 'label' in item && typeof item.label === 'string' ? item.label : item.value }]
  }), [modeField])
  const defaultMode = typeof modeField?.default === 'string' ? modeField.default : ''
  const currentMode = mode === 'all' ? '' : mode || runMode || defaultMode
  const modeNodeId = modeField?.comfy_node_id
  const visibleWorkflow = useMemo(() => currentMode && modeNodeId
    ? filterComfyBranch(workflow, modeNodeId, currentMode) : workflow, [workflow, modeNodeId, currentMode])
  const layout = useMemo(() => layoutComfyGraph(visibleWorkflow), [visibleWorkflow])
  const canvas = useMemo(() => grouped && nativeCanvas
    ? groupComfyNodes(layout.nodes, nativeCanvas, positions) : { nodes: layout.nodes, groups: undefined },
  [grouped, layout.nodes, nativeCanvas, positions])
  const defaultNode = detail?.service_name === 'nous-qwen21-all-in-one' && currentMode
    ? QWEN_MODE_NODES[currentMode] : undefined
  const graphFocus = useMemo(() => focus ?? (grouped && defaultNode && visibleWorkflow[defaultNode]
    ? { node: defaultNode } : undefined), [focus, grouped, defaultNode, visibleWorkflow])
  const query = search.trim().toLowerCase()
  const matchesNode = (id: string) => [id, `#${id}`, workflow[id]?.class_type, workflow[id]?._meta?.title]
    .some(value => value?.toLowerCase().includes(query))

  const usedCountByNode = useMemo(() => {
    const m = new Map<string, number>()
    for (const p of exposedParams) m.set(p.comfy_node_id, (m.get(p.comfy_node_id) ?? 0) + 1)
    return m
  }, [exposedParams])
  const modelIssueNodeIds = useMemo(() => new Set(modelIssues.map((i) => i.nodeId)), [modelIssues])
  const graphNodes = useMemo(
    () =>
      canvas.nodes.map((n) => ({
        ...n,
        usedCount: usedCountByNode.get(n.id) ?? 0,
        invalidModel: modelIssueNodeIds.has(n.id),
      })),
    [canvas.nodes, usedCountByNode, modelIssueNodeIds],
  )

  const rawRowsFor = (nodeId: string): RawInputRow[] => {
    const node = workflow[nodeId]
    if (!node) return []
    if (node.class_type === POWER_LORA_LOADER) {
      return [{ inputName: 'loras', rawValue: powerLoraRows(node.inputs ?? {}) }]
    }
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
      if (prev.some(param => param.comfy_node_id === nodeId && param.comfy_input === inputName)) return prev
      const classType = workflow[nodeId]?.class_type ?? ''
      const meta = deriveFieldMeta(classType, inputName, rawValue, objectInfo)
      const keysInUse = new Set(prev.map((p) => p.key))
      const base = keysInUse.has(inputName) ? `${inputName}_${nodeId}` : inputName
      let key = base
      for (let suffix = 2; keysInUse.has(key); suffix++) key = `${base}_${suffix}`
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

  const invalidateService = () => {
    void qc.invalidateQueries({ queryKey: ['services'] })
    if (serviceId != null) void qc.invalidateQueries({ queryKey: ['service', String(serviceId)] })
  }

  const save = async () => {
    setSaving(true)
    setSaved(false)
    setSaveError(null)
    try {
      const result = await putMapping(idStr, exposedParams, detail?.revision)
      setDetail(value => value ? { ...value, revision: result.revision } : value)
      setSaved(true)
      invalidateService()
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const remove = (key: string) => {
    const dependents = mappingDependents(exposedParams, key)
    if (dependents.length) {
      setSaveError(`「${key}」被 ${dependents.map(field => field.key).join('、')} 引用，请先调整这些字段`)
      return
    }
    setExposedParams(previous => previous.filter(field => field.key !== key))
    setStaleKeys(previous => previous.filter(item => item !== key))
    setSaved(false)
    setSaveError(null)
  }

  const selectNode = (id: string) => {
    setActiveNodeId(id)
    if (!visibleWorkflow[id]) setMode('all')
    setFocus({ node: id })
  }

  const moveNode = (id: string, position: { x: number; y: number }) => {
    const next = { ...positions, [id]: { ...position, classType: workflow[id].class_type } }
    setPositions(next)
    try { localStorage.setItem(`nous:comfy:positions:${idStr}`, JSON.stringify(next)) }
    catch { setSaveError('当前浏览器无法保存节点位置') }
  }

  if (loadError) {
    return <div style={{ fontSize: 12, color: 'var(--error, #ef4444)', padding: 14 }}>{loadError}</div>
  }
  if (!detail) {
    return <div style={{ fontSize: 12, color: 'var(--muted)', padding: 14 }}>加载中…</div>
  }

  const displayedFields = exposedParams.filter(field =>
    [field.key, field.label, field.comfy_input].some(value => value?.toLowerCase().includes(query)) ||
    matchesNode(field.comfy_node_id))

  return (
    <div className="comfy-editor">
      <div className="comfy-editor-header">
        <SidecarStatusLine health={health} />
        <ComfyWorkflowUpdate key={idStr} detail={detail} fields={exposedParams} busy={updating || saving}
          onBusy={setUpdating} onReview={setReviewing} onApplied={(next, addedNodes) => {
            setDetail(next)
            if (addedNodes) {
              setMode('all')
              setSearch('')
            }
            setExposedParams(next.exposed_params)
            setStaleKeys(next.stale_keys ?? [])
            setModelIssues(findInvalidModelRefs(next.workflow_json as ComfyWorkflow, objectInfo))
            setSaved(true)
            setSaveError(null)
            invalidateService()
          }} />
      </div>

      <div className="comfy-editor-heading">
        <strong>工作流参数映射 <small>{exposedParams.length} 个字段</small></strong>
        <div className="comfy-editor-actions">
          {saved && !saving && <span role="status">已保存</span>}
          {saveError && <span role="alert">{saveError}</span>}
          <button type="button" onClick={() => void save()} disabled={saving || updating || reviewing || !!staleKeys.length} style={saveBtnStyle(saving)}>
            {saving ? '保存中…' : '保存配置'}
          </button>
        </div>
      </div>

      {!!staleKeys.length && (
        <div role="alert" className="comfy-editor-warning">
          失效字段 · {staleKeys.join('、')}。可在字段列表移除，或上传时重新绑定。
        </div>
      )}

      <div className="comfy-editor-grid" hidden={reviewing} inert={saving || updating}>
        <section className="comfy-editor-panel" aria-label="工作流配置">
          <div className="comfy-editor-toolbar">
            <div className="comfy-editor-actions">
              <button type="button" aria-pressed={view === 'graph'} onClick={() => setView('graph')}>节点画布</button>
              <button type="button" aria-pressed={view === 'fields'} onClick={() => setView('fields')}>字段列表</button>
            </div>
            <input type="search" aria-label="搜索节点或字段" placeholder={view === 'graph' ? '搜索节点或 ID' : '搜索字段'}
              value={search} onChange={event => setSearch(event.currentTarget.value)}
              onKeyDown={event => {
                if (event.key !== 'Enter' || view !== 'graph' || !query) return
                const id = Object.keys(workflow).find(matchesNode)
                if (id) selectNode(id)
              }} />
          </div>

          {view === 'graph' ? (
            <>
              <div className="comfy-editor-toolbar">
                {!!availableModes.length && <label>显示范围 <select value={mode} onChange={event => {
                  setMode(event.currentTarget.value)
                  setFocus(undefined)
                }}>
                  <option value="">当前功能</option><option value="all">全部节点</option>
                  {availableModes.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
                </select></label>}
                {nativeCanvas && <label>布局 <select value={arrangement} onChange={event => {
                  setArrangement(event.currentTarget.value === 'compact' ? 'compact' : 'grouped')
                  setFocus(activeNodeId ? { node: activeNodeId } : undefined)
                }}>
                  <option value="grouped">原分组</option>
                  <option value="compact">紧凑排列</option>
                </select></label>}
                {grouped && !!canvas.groups?.length && <select aria-label="定位分组" value="" onChange={event => {
                  if (event.currentTarget.value !== '') setFocus({ group: Number(event.currentTarget.value) })
                }}>
                  <option value="">定位分组</option>
                  {canvas.groups.map((group, index) => <option key={index} value={index}>{group.title}</option>)}
                </select>}
                <span className="comfy-editor-count">{graphNodes.length} / {idSet.size} 个节点</span>
              </div>
              {query && <div className="comfy-editor-search-results">
                {Object.keys(workflow).filter(matchesNode).map(id => <button key={id} type="button" onClick={() => selectNode(id)}>
                  #{id} {workflow[id]._meta?.title || workflow[id].class_type}
                </button>)}
              </div>}
              <div className="comfy-editor-canvas">
                <ComfyTemplateGraph key={`${arrangement}:${currentMode}`}
                  nodes={graphNodes} edges={layout.edges} groups={canvas.groups} focus={graphFocus}
                  activeNodeId={activeNodeId} onNodeClick={setActiveNodeId} onMove={grouped ? moveNode : undefined} />
              </div>
            </>
          ) : (
            <div className="comfy-editor-field-list">
              {displayedFields.map(field => <div key={field.key} className="comfy-editor-field-row">
                <button type="button" disabled={!workflow[field.comfy_node_id]}
                  aria-pressed={activeNodeId === field.comfy_node_id} onClick={() => setActiveNodeId(field.comfy_node_id)}>
                  <strong>{field.label || field.key}</strong>
                  <code>{field.key} · #{field.comfy_node_id} · {field.comfy_input}</code>
                </button>
                <button type="button" aria-label={`移除参数 ${field.label || field.key}`} onClick={() => remove(field.key)}>移除参数</button>
              </div>)}
              {!displayedFields.length && <p className="comfy-editor-empty">没有匹配的字段</p>}
            </div>
          )}
        </section>

        <aside className="comfy-editor-panel" aria-label="映射详情">
          <div className="comfy-editor-toolbar"><strong>映射详情</strong>
            {activeNodeId && workflow[activeNodeId] && <button type="button" onClick={() => {
              setView('graph')
              selectNode(activeNodeId)
            }}>定位节点</button>}
          </div>
          {activeNodeId && activeNode ? (
            <NodeConfigPanel nodeId={activeNodeId} classType={activeNode.class_type} rows={activeRows}
              exposedByInput={exposedByInput} objectInfo={objectInfo}
              onToggle={(inputName, rawValue) => toggleExpose(activeNodeId, inputName, rawValue)}
              onPatch={(inputName, patch) => patchExposed(activeNodeId, inputName, patch)}
              onRemove={remove} modes={availableModes} onClose={() => setActiveNodeId(null)} />
          ) : <p className="comfy-editor-empty">选择节点后可添加、编辑或移除参数。</p>}
          <details className="comfy-editor-preview"><summary>运行表单预览</summary>
            <CanvasPreviewPanel exposedParams={exposedParams.filter(field => !staleKeys.includes(field.key))} />
          </details>
        </aside>
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
  label, value, onChange,
}: {
  label: string
  value: number | undefined
  onChange: (v: number | undefined) => void
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 10, color: 'var(--muted)', minWidth: 0 }}>
      {label}
      <input
        type="number"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
        style={{ ...smallInput, width: '100%', minWidth: 0 }}
      />
    </label>
  )
}

function optionValue(option: unknown): unknown {
  return option && typeof option === 'object' && 'value' in option ? option.value : option
}

function modeVisibilityPatch(
  field: ExposedParamDraft,
  shown: string[],
  next: string[],
  allModes: string[],
): Partial<ExposedParamDraft> {
  const visible = { ...field.visible_when }
  if (allModes.every(mode => next.includes(mode))) delete visible.input_mode
  else visible.input_mode = next

  const patch: Partial<ExposedParamDraft> = { visible_when: visible }
  if (field.required !== false) {
    patch.required = false
    patch.required_when = { input_mode: next }
  } else if (field.required_when) {
    const required = { ...field.required_when }
    const requiredModes = [required.input_mode].flat()
    const nextRequired = !required.input_mode || shown.every(mode => requiredModes.includes(mode))
      ? next
      : next.filter(mode => requiredModes.includes(mode))
    if (allModes.every(mode => nextRequired.includes(mode))) delete required.input_mode
    else required.input_mode = nextRequired
    if (nextRequired.length && Object.keys(required).length === 0) patch.required = true
    patch.required_when = nextRequired.length && Object.keys(required).length ? required : null
  }
  return patch
}

function NodeConfigPanel({
  nodeId, classType, rows, exposedByInput, objectInfo, onToggle, onPatch, onRemove, onClose, modes,
}: {
  nodeId: string
  classType: string
  rows: RawInputRow[]
  exposedByInput: Map<string, ExposedParamDraft>
  objectInfo: Record<string, unknown> | null
  onToggle: (inputName: string, rawValue: unknown) => void
  onPatch: (inputName: string, patch: Partial<ExposedParamDraft>) => void
  onRemove: (key: string) => void
  modes: { value: string; label: string }[]
  onClose: () => void
}) {
  return (
    <div className="comfy-node-config" key={nodeId}>
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
          {rows.some(row => !exposedByInput.has(row.inputName)) && (
            <small className="comfy-available-inputs">可配置输入 · 勾选后暴露给运行页与 API</small>
          )}
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
            const allModes = modes.map(mode => mode.value)
            const visible = cur?.visible_when?.input_mode
            const shown = visible == null ? allModes : [visible].flat().filter((value): value is string => typeof value === 'string')
            const allModesShown = allModes.every(mode => shown.includes(mode))
            const requiredModes = [cur?.required_when?.input_mode].flat()
            const modeRequired = !!cur?.required_when && Object.keys(cur.required_when).length === 1
              && shown.length > 0 && shown.every(mode => requiredModes.includes(mode))
            const loraFile = /^(?:lora_name|lora_\d+)$/i.test(r.inputName) && typeof r.rawValue === 'string'
            const directory = loraDirectory(cur?.key)
            const fileOptions = [...new Set((meta.options ?? cur?.options ?? []).map(optionValue)
              .filter((value): value is string => typeof value === 'string'
                && (!directory || value === 'None' || value.toLowerCase().startsWith(directory))))]
            const canHide = !cur || cur.hidden || (cur.required === false && !cur.required_when)
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
                    onChange={() => cur ? onRemove(cur.key) : onToggle(r.inputName, r.rawValue)}
                    style={{ marginTop: 2, cursor: 'pointer' }}
                  />
                  <span style={{ minWidth: 0, flex: 1 }}>
                    <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--text)' }}>{r.inputName}</span>
                    <span style={{ display: 'block', fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>
                      原值: <ValueBadge value={r.rawValue} />
                    </span>
                  </span>
                </label>

                {cur && <>
                <div className="comfy-draft-source"><span>API 参数</span><code>{cur.key}</code></div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input
                    aria-label="显示名称"
                    style={{ ...smallInput, flex: 1, minWidth: 0 }}
                    placeholder="显示名"
                    value={cur.label ?? r.inputName}
                    onChange={(e) => onPatch(r.inputName, { label: e.target.value })}
                  />
                  <select
                    aria-label="字段类型"
                    style={{ ...smallInput, width: 84, flexShrink: 0 }}
                    value={type} disabled={meta.type === 'lora_list'}
                    onChange={(e) => onPatch(r.inputName, { type: e.target.value })}
                  >
                    {meta.type === 'lora_list' ? <option value="lora_list">LoRA 列表</option> : TYPE_OPTIONS.map((o) => (
                      <option key={o.v} value={o.v}>{o.label}</option>
                    ))}
                  </select>
                </div>

                {type === 'lora_list' && <div className="comfy-draft-default">
                  <span>默认 LoRA</span>
                  <LoRAListField value={cur.default} directory={directory}
                    onChange={value => onPatch(r.inputName, { default: value })} />
                </div>}
                {!fileLike && type !== 'lora_list' && (
                  <label className="comfy-draft-default">默认值
                    {loraFile ? (
                      <select aria-label={`LoRA 文件 ${r.inputName}`} value={String(cur.default ?? '')}
                        disabled={fileOptions.length === 0}
                        onChange={event => {
                          const value = event.currentTarget.value
                          const options = cur.options?.length ? cur.options : fileOptions
                          onPatch(r.inputName, {
                            default: value,
                            options: options.some(option => optionValue(option) === value) ? options : [...options, value],
                          })
                        }}>
                        {!fileOptions.includes(String(cur.default ?? '')) && (
                          <option value={String(cur.default ?? '')} disabled>
                            {cur.default == null || cur.default === '' ? '请选择 LoRA 文件' : `${String(cur.default)}（未在清单中）`}
                          </option>
                        )}
                        {fileOptions.map(file => <option key={file} value={file}>{file}</option>)}
                      </select>
                    ) : type === 'boolean' ? <input type="checkbox" checked={Boolean(cur.default)}
                      onChange={event => onPatch(r.inputName, { default: event.currentTarget.checked })} />
                      : <input style={smallInput} type={numeric ? 'number' : 'text'} step={type === 'number' ? 'any' : 1}
                        value={typeof cur.default === 'number' || typeof cur.default === 'string' ? cur.default : ''}
                        onChange={event => {
                          const value = event.currentTarget.value
                          onPatch(r.inputName, { default: numeric ? (value === '' ? null : Number(value)) : value })
                        }} />}
                    {loraFile && fileOptions.length === 0 && <small>暂未获取 ComfyUI LoRA 文件清单</small>}
                  </label>
                )}
                <div className="comfy-draft-flags">
                  <label title={!canHide ? '取消必填后可隐藏' : undefined}><input type="checkbox" checked={!cur.hidden}
                    disabled={!canHide} onChange={event => onPatch(r.inputName, { hidden: !event.currentTarget.checked })} /> 在运行页暴露</label>
                  <label><input type="checkbox" checked={cur.required !== false || modeRequired} disabled={cur.omit_when_empty || cur.hidden}
                    onChange={event => {
                      const checked = event.currentTarget.checked
                      onPatch(r.inputName, {
                        required: checked && allModesShown,
                        required_when: checked && !allModesShown ? { input_mode: shown } : null,
                      })
                    }} /> {allModesShown ? '始终必填' : '显示时必填'}</label>
                </div>
                {modes.length > 0 && cur.key !== 'input_mode' && (
                  <fieldset className="comfy-mode-visibility"><legend>显示于</legend>
                    {modes.map(mode => {
                      const checked = shown.includes(mode.value)
                      return <label key={mode.value}><input type="checkbox" checked={checked}
                        disabled={checked && shown.length === 1} onChange={() => {
                          const next = checked ? shown.filter(value => value !== mode.value) : [...shown, mode.value]
                          onPatch(r.inputName, modeVisibilityPatch(cur, shown, next, allModes))
                        }} />{mode.label}</label>
                    })}
                  </fieldset>
                )}
                {numeric && (
                  <div className="comfy-numeric-bounds">
                    <NumField
                      label="min" value={cur.min ?? meta.min}
                      onChange={(v) => onPatch(r.inputName, { min: v })}
                    />
                    <NumField
                      label="max" value={cur.max ?? meta.max}
                      onChange={(v) => onPatch(r.inputName, { max: v })}
                    />
                    <NumField
                      label="step" value={cur.step ?? meta.step}
                      onChange={(v) => onPatch(r.inputName, { step: v })}
                    />
                  </div>
                )}

                {seedLike && (
                  <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text)' }}>
                    <input
                      type="checkbox"
                      aria-label={`随机 ${r.inputName}`}
                      checked={!!cur.random}
                      onChange={(e) => onPatch(r.inputName, { random: e.target.checked })}
                    />
                    未传种子时随机取值
                  </label>
                )}

                {fileLike && (
                  <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text)' }}>
                    <input
                      type="checkbox"
                      aria-label={`未传时剪掉 ${r.inputName}`}
                      checked={!!cur.omit_when_empty}
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
                <button className="comfy-remove-field" type="button" onClick={() => onRemove(cur.key)}>移除参数</button>
                </>}
              </div>
            )
          })}
        </div>
    </div>
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
  if (p.options) {
    constraints.enum = p.options.map((option) => (
      option && typeof option === 'object' && 'value' in option ? option.value : option
    ))
    const metadata = p.options.filter((option) => option && typeof option === 'object' && 'value' in option)
    if (metadata.length) constraints.option_meta = metadata
  }
  if (p.visible_when) constraints.visible_when = p.visible_when
  if (p.required_when) constraints.required_when = p.required_when
  if (p.widget) constraints.widget = p.widget
  if (p.hidden) constraints.hidden = true
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
  const [edits, setValues] = useState<Record<string, unknown>>({})
  const values = Object.fromEntries(schemaParams.flatMap(param => {
    const key = paramKey(param)
    return key ? [[key, key in edits ? edits[key] : defaultFor(param)]] : []
  }))

  return (
    <div data-testid="canvas-preview-panel" style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '14px' }}>
        {schemaParams.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--muted)', textAlign: 'center', padding: '20px 4px' }}>
            暂无暴露字段 — 在右侧节点图点节点卡配置
          </div>
        ) : (
          schemaParams.filter((p) => isFieldVisible(p, values)).map((p) => {
            const k = paramKey(p)
            return (
              <Field
                key={k ?? `${p.node_id}.${p.input_name}`}
                param={{ ...p, required: isFieldRequired(p, values) }}
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

function saveBtnStyle(saving: boolean): React.CSSProperties {
  return {
    fontSize: 12, padding: '6px 14px', background: 'var(--accent)', color: 'var(--on-accent)',
    border: 'none', borderRadius: 4, cursor: saving ? 'not-allowed' : 'pointer',
    opacity: saving ? 0.6 : 1,
  }
}
