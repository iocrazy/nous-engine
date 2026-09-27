// 工作流 → WebUI 应用编辑器的纯逻辑核心(spec 2026-06-09 PR-2)。
// 把 nous 节点的 widget 派生成 ExposedParam(对外表单字段);节点无声明式
// 定义时回退到 data 键 + 值推断。React Flow 渲染层在 WorkflowAppEditor.tsx。
import type { ExposedParam } from '../../api/services'
import { DECLARATIVE_NODES, type WidgetDef } from '../../models/nodeRegistry'

export interface EditorNodeLike {
  id: string
  type: string
  data?: Record<string, unknown>
  position?: { x: number; y: number }
}

function optionsToEnum(
  options?: WidgetDef['options'],
): { enum: string[]; enum_labels?: Record<string, string> } | null {
  if (!options || options.length === 0) return null
  const en: string[] = []
  const labels: Record<string, string> = {}
  let hasLabels = false
  for (const o of options) {
    if (typeof o === 'string') {
      en.push(o)
    } else {
      en.push(String(o.value))
      if (o.label && o.label !== o.value) {
        labels[String(o.value)] = o.label
        hasLabels = true
      }
    }
  }
  return hasLabels ? { enum: en, enum_labels: labels } : { enum: en }
}

/** 一个 widget → 一个 ExposedParam。类型/约束直接取自 widget 定义(nous 强类型,
 *  无需像 Infinite-Canvas 那样靠字段名猜)。`input_name` = node.data 字段名。 */
export function widgetToExposed(
  nodeId: string,
  widget: WidgetDef,
  node?: EditorNodeLike,
): ExposedParam {
  const name = widget.name
  const cur = node?.data?.[name]
  const base: ExposedParam = {
    node_id: nodeId,
    key: name,
    input_name: name,
    label: widget.label || name,
    type: 'string',
    required: false,
    default: cur ?? widget.default,
  }
  switch (widget.widget) {
    case 'slider': {
      const constraints: Record<string, number> = {}
      if (typeof widget.min === 'number') constraints.min = widget.min
      if (typeof widget.max === 'number') constraints.max = widget.max
      if (typeof widget.step === 'number') constraints.step = widget.step
      return { ...base, type: widget.precision === 0 ? 'integer' : 'number', constraints }
    }
    case 'checkbox':
      return { ...base, type: 'boolean' }
    case 'image_upload':
      return { ...base, type: 'image' }
    case 'input':
      return { ...base, type: 'string', constraints: { format: 'single_line' } }
    case 'textarea':
      return { ...base, type: 'string' }
    case 'select':
    case 'model_select':
    case 'agent_select': {
      const en = optionsToEnum(widget.options)
      return { ...base, type: 'string', constraints: en ?? {} }
    }
    default:
      return base
  }
}

/** 无声明式定义节点的回退:把 data 键暴露成字段,类型按值推断(仅 fallback)。 */
export function fallbackExposed(nodeId: string, key: string, value: unknown): ExposedParam {
  let type = 'string'
  let constraints: Record<string, unknown> = { format: 'single_line' }
  if (typeof value === 'boolean') {
    type = 'boolean'
    constraints = {}
  } else if (typeof value === 'number') {
    type = Number.isInteger(value) ? 'integer' : 'number'
    constraints = {}
  }
  return {
    node_id: nodeId,
    key,
    input_name: key,
    label: key,
    type,
    required: false,
    default: value,
    constraints,
  }
}

export interface ExposableRow {
  input_name: string
  label: string
  widget?: WidgetDef
  param: ExposedParam
}

/** 一个节点上「可勾选暴露」的行集合(给节点卡片渲染 checkbox 用)。 */
export function exposableRowsFor(node: EditorNodeLike): ExposableRow[] {
  const def = DECLARATIVE_NODES[node.type]
  if (def && def.widgets.length > 0) {
    return def.widgets.map((w) => ({
      input_name: w.name,
      label: w.label || w.name,
      widget: w,
      param: widgetToExposed(node.id, w, node),
    }))
  }
  // fallback:跳过数组值(那是上游连线 [nodeId, idx],不是用户可填值)。
  const data = node.data ?? {}
  return Object.entries(data)
    .filter(([, v]) => !Array.isArray(v))
    .map(([k, v]) => ({ input_name: k, label: k, param: fallbackExposed(node.id, k, v) }))
}

/** 跨节点 key 冲突(如多个节点都暴露 `seed`)→ 加短 node-id 前缀去重。
 *  `key` = 调用方字段名,必须唯一。 */
export function dedupeKeys(params: ExposedParam[]): ExposedParam[] {
  const seen = new Set<string>()
  return params.map((p) => {
    let k = p.key || p.input_name || 'field'
    if (seen.has(k)) k = `${String(p.node_id).slice(0, 4)}_${k}`
    let i = 2
    while (seen.has(k)) k = `${k}_${i++}`
    seen.add(k)
    return p.key === k ? p : { ...p, key: k }
  })
}

/** 一个 ExposedParam 在草稿里的稳定身份 = node_id + input_name。 */
export function paramId(p: { node_id: string; input_name?: string }): string {
  return `${p.node_id}::${p.input_name ?? ''}`
}

/** 零依赖分层布局:服务页快照没有 position(spec R1),按 edges 最长路径求列。 */
export function layeredLayout(
  nodes: { id: string }[],
  edges: { source: string; target: string }[],
  opts?: { colGap?: number; rowGap?: number },
): Record<string, { x: number; y: number }> {
  const COL = opts?.colGap ?? 360
  const ROW = opts?.rowGap ?? 180
  const ids = nodes.map((n) => n.id)
  const idset = new Set(ids)
  const incoming: Record<string, string[]> = {}
  ids.forEach((id) => (incoming[id] = []))
  for (const e of edges) {
    if (idset.has(e.source) && idset.has(e.target)) incoming[e.target].push(e.source)
  }
  const depth: Record<string, number> = {}
  const visiting = new Set<string>()
  const computeDepth = (id: string): number => {
    if (depth[id] !== undefined) return depth[id]
    if (visiting.has(id)) return 0 // 环保护
    visiting.add(id)
    let d = 0
    for (const p of incoming[id]) d = Math.max(d, computeDepth(p) + 1)
    visiting.delete(id)
    depth[id] = d
    return d
  }
  ids.forEach(computeDepth)
  const byCol: Record<number, string[]> = {}
  ids.forEach((id) => (byCol[depth[id]] ??= []).push(id))
  const pos: Record<string, { x: number; y: number }> = {}
  for (const [col, list] of Object.entries(byCol)) {
    list.forEach((id, row) => (pos[id] = { x: Number(col) * COL, y: row * ROW }))
  }
  return pos
}
