import { memo, useEffect, useEffectEvent, useMemo, useState } from 'react'
import {
  ReactFlow, Background, Controls, Handle, Position, useNodesState,
  type Edge, type Node, type NodeMouseHandler, type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { ComfyCanvasLayout } from '../../api/comfyTemplates'
import type { ComfyLayoutEdge, ComfyLayoutNode } from './comfyGraphLayout'

const USED_COLOR = 'var(--accent-2)'
const ACTIVE_COLOR = 'var(--accent)'
const ERROR_COLOR = 'var(--error, #ef4444)'

interface ComfyCardData {
  classType: string
  title?: string
  nodeId: string
  usedCount: number
  active: boolean
  /** 该节点有原始输入的值是 combo/enum 但不在 object_info 取值表里(见
   *  workflowModelCheck.ts)——多半是导入了别的机器冻结下来的模型文件名/路径。 */
  invalidModel?: boolean
  [key: string]: unknown
}

// React Flow requires handles even when the card does not display ports.
const HANDLE_STYLE = {
  width: 1, height: 1, minWidth: 1, minHeight: 1,
  background: 'transparent', border: 'none', opacity: 0, pointerEvents: 'none' as const,
}

function ComfyNodeCardImpl({ data }: { data: ComfyCardData }) {
  const used = data.usedCount > 0
  const invalidModel = !!data.invalidModel
  const ringColor = data.active ? ACTIVE_COLOR : invalidModel ? ERROR_COLOR : used ? USED_COLOR : null
  return (
    <div
      title={data.classType}
      style={{
        width: 190,
        background: 'var(--card)',
        border: `1px solid ${ringColor ?? 'var(--border-strong, var(--border))'}`,
        borderRadius: 'var(--node-radius, 14px)',
        overflow: 'hidden',
        boxShadow: data.active
          ? `var(--shadow-md), 0 0 0 2px ${ACTIVE_COLOR}`
          : invalidModel
            ? `var(--shadow-md), 0 0 0 1px ${ERROR_COLOR}`
            : used
              ? `var(--shadow-md), 0 0 0 1px ${USED_COLOR}`
              : 'var(--shadow-md)',
        fontSize: 12,
        cursor: 'pointer',
      }}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Right} isConnectable={false} style={HANDLE_STYLE} />
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, padding: '7px 10px',
        background: 'var(--card-hl)', borderRadius: 'var(--node-radius, 14px) var(--node-radius, 14px) 0 0',
        position: 'relative', overflow: 'hidden',
      }}>
        <span style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 3, background: ringColor ?? 'var(--border-strong, var(--border))' }} />
        <span style={{
          flex: 1, color: 'var(--text)', fontWeight: 700, fontSize: 10.5,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {data.title || data.classType}
        </span>
      </div>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6,
        padding: '7px 10px', borderTop: '1px solid var(--border)', color: 'var(--muted)',
      }}>
        <code style={{ fontFamily: 'var(--mono, monospace)', fontSize: 10.5 }}>#{data.nodeId}</code>
        {invalidModel ? (
          <span
            title="有原始字段的值不在本机模型库里，点开检查/修正"
            style={{
              fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 999,
              background: 'color-mix(in srgb, ' + ERROR_COLOR + ' 16%, transparent)', color: ERROR_COLOR,
            }}
          >
            模型引用无效
          </span>
        ) : used ? (
          <span style={{
            fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 999,
            background: 'color-mix(in srgb, ' + USED_COLOR + ' 16%, transparent)', color: USED_COLOR,
          }}>
            {data.usedCount} 已用
          </span>
        ) : (
          <span style={{ fontSize: 10.5 }}>点击配置</span>
        )}
      </div>
    </div>
  )
}

const ComfyNodeCard = memo(ComfyNodeCardImpl)
const nodeTypes = {
  comfyNode: ComfyNodeCard,
  canvasGroup: ({ data }: { data: ComfyCardData }) => <strong className="comfy-canvas-group-label">{data.title}</strong>,
}

export interface ComfyTemplateGraphProps {
  nodes: Array<ComfyLayoutNode & { invalidModel?: boolean }>
  edges: ComfyLayoutEdge[]
  activeNodeId: string | null
  groups?: ComfyCanvasLayout['groups']
  focus?: { group?: number; node?: string }
  onMove?: (nodeId: string, position: { x: number; y: number }) => void
  onNodeClick: (nodeId: string) => void
}

export default function ComfyTemplateGraph({
  nodes, edges, activeNodeId, onNodeClick, groups, focus, onMove,
}: ComfyTemplateGraphProps) {
  const initialNodes = useMemo<Node<ComfyCardData>[]>(() => [
    ...(groups ?? []).map(({ title, bounding: [x, y, width, height] }, index) => ({
      id: `group:${index}`,
      type: 'canvasGroup',
      position: { x, y },
      width, height,
      className: 'comfy-canvas-group',
      selectable: false,
      draggable: false,
      focusable: false,
      zIndex: -1,
      data: { title, classType: '', nodeId: '', usedCount: 0, active: false },
    })),
    ...nodes.map((node) => ({
      id: node.id,
      type: 'comfyNode',
      position: { x: node.x, y: node.y },
      data: {
        classType: node.class_type,
        title: node.title,
        nodeId: node.id,
        usedCount: node.usedCount,
        active: node.id === activeNodeId,
        invalidModel: node.invalidModel,
      },
    })),
  ], [nodes, activeNodeId, groups])
  const [rfNodes, setNodes, onNodesChange] = useNodesState(initialNodes)
  const [viewport, setViewport] = useState<ReactFlowInstance<Node<ComfyCardData>> | null>(null)

  useEffect(() => setNodes(initialNodes), [initialNodes, setNodes])
  const moveViewport = useEffectEvent((view: ReactFlowInstance<Node<ComfyCardData>>, targetFocus: NonNullable<typeof focus>) => {
    const focus = targetFocus
    if (focus.group !== undefined && groups?.[focus.group]) {
      const [x, y, width, height] = groups[focus.group].bounding
      void view.fitBounds({ x, y, width, height }, { padding: 0.12, duration: 160 })
    } else if (focus.node) {
      const target = nodes.find(node => node.id === focus.node)
      if (target) void view.setCenter(target.x + 95, target.y + 30, { zoom: 0.9, duration: 160 })
    }
  })
  useEffect(() => {
    if (viewport && focus) moveViewport(viewport, focus)
  }, [viewport, focus])

  const rfEdges = useMemo<Edge[]>(() => {
    // 同一对节点之间常有多条边(如 CheckpointLoader 同时喂 model 和 clip 给
    // LoraLoader)。我们的卡片只有一进一出两个 handle,这些边会算出完全重合的路径,
    // 叠加描边只会让那几根线莫名比别人粗/深。按 source→target 去重。
    const seen = new Set<string>()
    const out: Edge[] = []
    for (const e of edges) {
      const key = `${e.source}->${e.target}`
      if (seen.has(key)) continue
      seen.add(key)
      out.push({
        id: `e-${e.source}-${e.target}`,
        source: e.source,
        target: e.target,
        // 颜色走主题 token --graph-edge(theme.css 里按对比度挑的,亮/暗各一套)。
        // 别再换回 --border / --border-strong / --muted —— 那三个都试过,总有一个
        // 主题下是隐形的。stroke-linecap: round 让密集交叉的曲线端点不出方角。
        // 边渲染层在 xyflow DOM 里先于节点层,天然在节点后面,不用 z-index。
        style: { stroke: [e.source, e.target].includes(activeNodeId ?? '') ? ACTIVE_COLOR : 'var(--graph-edge)', strokeWidth: 1.8, strokeLinecap: 'round' as const },
      })
    }
    return out
  }, [edges, activeNodeId])

  const handleNodeClick: NodeMouseHandler = (_, node) => {
    if (node.type === 'comfyNode') onNodeClick(node.id)
  }

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      nodeTypes={nodeTypes}
      onNodeClick={handleNodeClick}
      onInit={setViewport}
      onNodesChange={onNodesChange}
      onNodeDragStop={(_, node) => onMove?.(node.id, node.position)}
      nodesDraggable={!!onMove}
      minZoom={0.03}
      nodesConnectable={false}
      elementsSelectable={false}
      fitView={!focus}
      proOptions={{ hideAttribution: true }}
      style={{ background: 'var(--bg)' }}
    >
      <Background gap={24} size={1.4} color="var(--grid, var(--border))" />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}
