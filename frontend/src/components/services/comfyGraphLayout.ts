// 通用 ComfyUI 工作流(API 格式)→ React Flow 布局(Task 9,spec §「读图选节点配置」)。
// 纯逻辑,不碰 React/xyflow —— 渲染层在 ComfyTemplateGraph.tsx。
// 布局:按拓扑深度分列(x=深度×COL_WIDTH),同列内按序排(y=序×ROW_HEIGHT)。边 = inputs
// 里形如 [nodeId, slot] 的引用值(ComfyUI API 格式的连线表示)。usedCount 这里恒为 0,由
// 调用方(ComfyTemplateEditor,按 exposedParams 里 comfy_node_id 计数)回填,布局本身不
// 关心暴露态。
//
// 间距对齐 Infinite-Canvas 参考实现(static/js/comfyui-settings.js renderGraph,
// NODE_W/X_GAP/NODE_H/Y_GAP)的比例(gap ≈ 节点尺寸的 ~28%),按我们卡片实际宽/高
// (ComfyTemplateGraph.tsx 卡片宽 190、两行内容高约 60)换算 —— 旧的 220/64 几乎不留
// 缝隙,图挤成一团(用户反馈「太乱」的一部分原因),这里放宽出可读的行列间距。

import type { ComfyCanvasLayout } from '../../api/comfyTemplates'

export interface ComfyWorkflowNode {
  class_type: string
  inputs: Record<string, unknown>
  _meta?: { title?: string }
}

export type ComfyWorkflow = Record<string, ComfyWorkflowNode>

export interface ComfyLayoutNode {
  id: string
  class_type: string
  x: number
  y: number
  usedCount: number
  title?: string
}

export interface ComfyLayoutEdge {
  source: string
  target: string
}

export const COL_WIDTH = 250
export const ROW_HEIGHT = 80

/** ComfyUI API 格式里,一个节点引用另一个节点输出的值形如 `[nodeId, slotIndex]`
 *  (nodeId 是字符串、且必须在同一工作流的节点表里,slotIndex 是数字)——用来把它跟
 *  「碰巧是二元数组的普通标量输入」(极少见,但不能靠 length 误判)区分开。 */
export function isComfyNodeRef(value: unknown, nodeIds: Set<string>): value is [string, number] {
  return (
    Array.isArray(value) &&
    value.length === 2 &&
    typeof value[0] === 'string' &&
    typeof value[1] === 'number' &&
    nodeIds.has(value[0])
  )
}

export function layoutComfyGraph(
  workflow: ComfyWorkflow,
): { nodes: ComfyLayoutNode[]; edges: ComfyLayoutEdge[] } {
  const ids = Object.keys(workflow)
  const idSet = new Set(ids)
  const incoming: Record<string, string[]> = {}
  ids.forEach((id) => (incoming[id] = []))

  const edges: ComfyLayoutEdge[] = []
  for (const id of ids) {
    const inputs = workflow[id]?.inputs ?? {}
    for (const value of Object.values(inputs)) {
      if (isComfyNodeRef(value, idSet)) {
        incoming[id].push(value[0])
        edges.push({ source: value[0], target: id })
      }
    }
  }

  // BFS/DFS 深度:每节点深度 = 其引用来源里最大深度 + 1(无引用来源 → 0)。
  // 环保护(visiting set)防止循环引用（工作流本不该有环，但读图别崩）把深度当 0 兜底。
  const depth: Record<string, number> = {}
  const visiting = new Set<string>()
  const computeDepth = (id: string): number => {
    if (depth[id] !== undefined) return depth[id]
    if (visiting.has(id)) return 0
    visiting.add(id)
    let d = 0
    for (const parent of incoming[id]) d = Math.max(d, computeDepth(parent) + 1)
    visiting.delete(id)
    depth[id] = d
    return d
  }
  ids.forEach(computeDepth)

  const byCol: Record<number, string[]> = {}
  ids.forEach((id) => (byCol[depth[id]] ??= []).push(id))

  const nodes: ComfyLayoutNode[] = []
  for (const [col, list] of Object.entries(byCol)) {
    list.forEach((id, row) => {
      nodes.push({
        id,
        class_type: workflow[id].class_type,
        title: workflow[id]._meta?.title,
        x: Number(col) * COL_WIDTH,
        y: row * ROW_HEIGHT,
        usedCount: 0,
      })
    })
  }

  return { nodes, edges }
}


export interface ComfyNodePosition {
  classType: string
  x: number
  y: number
}

export type ComfyNodePositions = Record<string, ComfyNodePosition>

export function groupComfyNodes(
  nodes: ComfyLayoutNode[],
  canvas: ComfyCanvasLayout,
  positions: ComfyNodePositions,
) {
  const native = new Map(canvas.nodes.map(node => [node.id, node]))
  const right = Math.max(0, ...canvas.groups.map(({ bounding: [x, , width] }) => x + width),
    ...canvas.nodes.map(({ pos }) => pos[0] + 190))
  const top = Math.min(0, ...canvas.nodes.map(({ pos }) => pos[1]))
  const extra: ComfyLayoutNode[] = []
  const placed = nodes.map(node => {
    const manual = positions[node.id]
    const original = native.get(node.id)
    if (original?.type === node.class_type) {
      const [x, y] = original.pos
      return { ...node, ...(manual?.classType === node.class_type ? manual : { x, y }) }
    }
    const index = extra.length
    const fallback = { x: right + 120 + index % 3 * COL_WIDTH, y: top + 50 + Math.floor(index / 3) * ROW_HEIGHT }
    const placedNode = { ...node, ...(manual?.classType === node.class_type ? manual : fallback) }
    extra.push(placedNode)
    return placedNode
  })
  const groups = canvas.groups.filter(({ bounding: [x, y, w, h] }) =>
    placed.some(node => {
      const match = native.get(node.id)
      return match?.type === node.class_type && match.pos[0] >= x && match.pos[0] <= x + w
        && match.pos[1] >= y && match.pos[1] <= y + h
    }),
  )
  if (extra.length) {
    const x = Math.min(...extra.map(node => node.x)) - 24
    const y = Math.min(...extra.map(node => node.y)) - 42
    const w = Math.max(...extra.map(node => node.x + 214)) - x
    const h = Math.max(...extra.map(node => node.y + ROW_HEIGHT)) - y
    groups.push({ title: '服务接入', bounding: [x, y, w, h] })
  }
  return { nodes: placed, groups }
}

const OUTPUT_NODES = /^(SaveImage|Image Save|SaveVideo|SaveWEBM|SaveAnimated|VHS_VideoCombine)/

export function filterComfyBranch(workflow: ComfyWorkflow, modeNodeId: string, mode: string): ComfyWorkflow {
  const ids = new Set(Object.keys(workflow))
  const outputs = Object.keys(workflow).filter(id => OUTPUT_NODES.test(workflow[id].class_type))
  if (!outputs.length) return workflow
  const switchValue = (input: unknown) => {
    if (typeof input === 'boolean') return input
    if (!isComfyNodeRef(input, ids)) return undefined
    const node = workflow[input[0]]
    const source = node.inputs?.string_a
    if (node.class_type === 'StringCompare' && isComfyNodeRef(source, ids) && source[0] === modeNodeId
      && node.inputs.mode === 'Equal' && typeof node.inputs.string_b === 'string') {
      return node.inputs.string_b === mode
    }
    return undefined
  }
  const included = new Set<string>()
  const visit = (id: string) => {
    if (included.has(id)) return
    included.add(id)
    const node = workflow[id]
    const inputs = node.inputs ?? {}
    const toggle = node.class_type === 'ComfySwitchNode' ? switchValue(inputs.switch) : undefined
    const sources = toggle === undefined ? Object.values(inputs) : [inputs.switch, toggle ? inputs.on_true : inputs.on_false]
    for (const source of sources) if (isComfyNodeRef(source, ids)) visit(source[0])
  }
  outputs.forEach(visit)
  return Object.fromEntries(Object.entries(workflow).filter(([id]) => included.has(id)))
}
