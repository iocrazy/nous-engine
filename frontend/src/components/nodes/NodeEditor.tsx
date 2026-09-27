import { useCallback, useRef, useMemo, useEffect, useState, lazy, Suspense } from 'react'
import { Copy, Ban, Trash2, Group, Ungroup, Pencil, Download } from 'lucide-react'
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  SelectionMode,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  type Connection,
  type Node,
  type Edge,
  type NodeChange,
  type EdgeChange,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { nodeTypes } from './nodeTypes'
import GroupLayer from './GroupLayer'
import ShortcutsHelp from './ShortcutsHelp'
import Lightbox from './Lightbox'
import NodeCreateMenu from './NodeCreateMenu'
import { choicesAcceptingInput, choicesProvidingOutput, firstInputHandle, firstOutputHandle, getAllChoices, type NodeChoice } from './nodeChoices'
import { computeGroupBounds } from './groupGeometry'
import PortTypedEdge from '../edges/PortTypedEdge'
import { PORT_TYPE_COLORS } from './portColors'
import { useWorkspaceStore } from '../../stores/workspace'
import { usePanelStore } from '../../stores/panel'
import { useExecutionStore } from '../../stores/execution'
import { NODE_DEFS, type NodeType, type PortType, type WorkflowNode, type WorkflowEdge } from '../../models/workflow'
import { buildPastedGraph } from '../../utils/pasteGraph'
import OverlayLoading from '../common/OverlayLoading'
import NodeLibraryPanel from '../panels/NodeLibraryPanel'
import NodePropertyPanel from '../panels/NodePropertyPanel'
import WorkflowsPanel from '../panels/WorkflowsPanel'
import PresetsPanel from '../panels/PresetsPanel'
import { useSelectionStore } from '../../stores/selection'
// 页面级 overlay 全部懒加载(review 前端 P1):这些只在 activeOverlay 命中时条件渲染,
// 却曾被静态 import 拖进首屏 —— 连带 recharts/d3(Usage/Dashboard/Status)一并进首屏包。
// 改 React.lazy 后按需 fetch,首屏只留画布核心(xyflow 保留静态,它是画布本体)。
const DashboardOverlay = lazy(() => import('../overlays/DashboardOverlay'))
const ModelsOverlay = lazy(() => import('../overlays/ModelsOverlay'))
const SettingsOverlay = lazy(() => import('../overlays/SettingsOverlay'))
const PresetDetailOverlay = lazy(() => import('../overlays/PresetDetailOverlay'))
const AgentManagementOverlay = lazy(() => import('../overlays/AgentManagementOverlay'))
const LogsOverlay = lazy(() => import('../overlays/LogsOverlay'))
const HistoryOverlay = lazy(() => import('../overlays/HistoryOverlay'))
const StatusOverlay = lazy(() => import('../overlays/StatusOverlay'))
const NodePackagesOverlay = lazy(() => import('../overlays/NodePackagesOverlay'))
const ServicesList = lazy(() => import('../../pages/ServicesList'))
const ServiceDetailRoute = lazy(() => import('../../pages/ServiceDetailRoute'))
const WorkflowsList = lazy(() => import('../../pages/WorkflowsList'))
const UsagePage = lazy(() => import('../../pages/UsagePage'))
const ApiKeysList = lazy(() => import('../../pages/ApiKeysList'))
const ApiKeyDetail = lazy(() => import('../../pages/ApiKeyDetail'))
function getPortType(nodeType: string, handleId: string | null | undefined): PortType | null {
  const def = NODE_DEFS[nodeType as NodeType]
  if (!def || !handleId) return null
  const allPorts = [...def.inputs, ...def.outputs]
  const port = allPorts.find((p) => p.id === handleId)
  return port?.type ?? null
}

const PANEL_MAP: Record<string, React.FC> = {
  nodes: NodeLibraryPanel,
  workflows: WorkflowsPanel,
  presets: PresetsPanel,
}

export default function NodeEditor() {
  const workflow = useWorkspaceStore((s) => s.getActiveWorkflow())
  const setWorkflow = useWorkspaceStore((s) => s.setWorkflow)
  const storeAddEdge = useWorkspaceStore((s) => s.addEdge)
  const storeRemoveEdge = useWorkspaceStore((s) => s.removeEdge)
  const storeAddNode = useWorkspaceStore((s) => s.addNode)
  const storeAddNodesWithEdges = useWorkspaceStore((s) => s.addNodesWithEdges)
  const storeSpliceNodeOnEdge = useWorkspaceStore((s) => s.spliceNodeOnEdge)
  const storeRemoveNode = useWorkspaceStore((s) => s.removeNode)
  const updateNode = useWorkspaceStore((s) => s.updateNode)
  const storeAddGroup = useWorkspaceStore((s) => s.addGroup)
  const storeRemoveGroup = useWorkspaceStore((s) => s.removeGroup)
  const undo = useWorkspaceStore((s) => s.undo)
  const redo = useWorkspaceStore((s) => s.redo)
  const { activePanel, activeOverlay, panelWidth } = usePanelStore()
  const nodeStates = useExecutionStore((s) => s.nodeStates)

  const reactFlowWrapper = useRef<HTMLDivElement>(null)
  const reactFlowInstance = useRef<ReactFlowInstance | null>(null)
  const setSelectedNodeId = useSelectionStore((s) => s.setSelectedNodeId)
  // 节点右键菜单(承载 旁路/复制/删除,让快捷键功能可发现)。坐标为画布容器内像素。
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; nodeId: string } | null>(null)
  // 端口拖到空白 → 快捷建相连节点菜单(借鉴 Infinite-Canvas)。
  const connectStartRef = useRef<{ nodeId: string; handleId: string | null; handleType: 'source' | 'target' } | null>(null)
  const [createMenu, setCreateMenu] = useState<{
    x: number; y: number; flowX: number; flowY: number
    fromNodeId: string; fromHandle: string | null; handleType: 'source' | 'target'
    portType: PortType; choices: NodeChoice[]
  } | null>(null)
  // 画布空白右键 → 光标处快捷建节点(无连接,借鉴 Infinite-Canvas 的 create 菜单)。
  const [paneMenu, setPaneMenu] = useState<{ x: number; y: number; flowX: number; flowY: number } | null>(null)

  // Cmd/Ctrl+Z undo; Cmd/Ctrl+Shift+Z (or Ctrl+Y) redo. Swallow when the
  // event target is an input/textarea/contenteditable so native text edit
  // history keeps working inside the portal editor, node inputs, etc.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      if (!mod) return
      const tgt = e.target as HTMLElement | null
      if (tgt) {
        const tag = tgt.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tgt.isContentEditable) return
      }
      const k = e.key.toLowerCase()
      if (k === 'z' && !e.shiftKey) {
        e.preventDefault()
        undo()
      } else if ((k === 'z' && e.shiftKey) || k === 'y') {
        e.preventDefault()
        redo()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo])

  const NODE_STATE_CLASS: Record<string, string> = {
    pending: 'node-pending',
    running: 'node-running',
    completed: 'node-completed',
    error: 'node-error',
  }

  const rfNodes: Node[] = useMemo(
    () =>
      workflow.nodes.map((n) => ({
        id: n.id,
        type: n.type,
        position: n.position,
        data: n.data,
        style: (n as any).style ?? { width: 320 },
        ...((n as any).width != null ? { width: (n as any).width } : {}),
        ...((n as any).height != null ? { height: (n as any).height } : {}),
        // 旁路态(node-bypassed)叠加在执行态之上 —— 旁路节点不会进入 running,
        // 故一般只会单独出现;两者都在时类名都挂,CSS 旁路优先(灰显)。
        className: [
          nodeStates[n.id] ? NODE_STATE_CLASS[nodeStates[n.id]] : '',
          n.data?.bypassed ? 'node-bypassed' : '',
        ].filter(Boolean).join(' ') || undefined,
      })),
    [workflow.nodes, nodeStates],
  )

  // 节点 id → type:给 PortTypedEdge 按 source 端口推 PortType 着色用。
  const nodeTypeById = useMemo(
    () => Object.fromEntries(workflow.nodes.map((n) => [n.id, n.type])) as Record<string, string>,
    [workflow.nodes],
  )

  const rfEdges: Edge[] = useMemo(
    () =>
      workflow.edges.map((e) => {
        // source 端口类型 → 颜色(复用端口圆点配色)。推不出则回退 muted。
        const portType = getPortType(nodeTypeById[e.source] ?? '', e.sourceHandle)
        const color = portType
          ? PORT_TYPE_COLORS[portType] ?? 'var(--muted-strong)'
          : 'var(--muted-strong)'
        return {
          id: e.id,
          source: e.source,
          sourceHandle: e.sourceHandle,
          target: e.target,
          targetHandle: e.targetHandle,
          type: 'portTyped',
          data: { color, portType },
        }
      }),
    [workflow.edges, nodeTypeById],
  )

  const edgeTypes = useMemo(() => ({ portTyped: PortTypedEdge }), [])

  const [nodes, setNodes, onNodesChangeInternal] = useNodesState(rfNodes)
  const [edges, setEdges, onEdgesChangeInternal] = useEdgesState(rfEdges)

  // Keep a ref to always access latest React Flow nodes/edges (avoids stale closures)
  const nodesRef = useRef(nodes)
  nodesRef.current = nodes
  const edgesRef = useRef(edges)
  edgesRef.current = edges

  // 复制/粘贴剪贴板(模块外不共享 —— 仅本会话内存;跨 tab 粘贴可用,因为是 ref
  // 持有的纯数据)。pasteSeq 让连续粘贴递增偏移,避免叠在同一处。
  const clipboardRef = useRef<{
    nodes: Array<{ id: string; type: string; data: Record<string, unknown>; position: { x: number; y: number }; style?: unknown; width?: number; height?: number }>
    edges: Array<{ source: string; sourceHandle: string; target: string; targetHandle: string }>
  } | null>(null)
  const pasteSeqRef = useRef(0)

  // Sync Zustand store changes back to React Flow, preserving resize dimensions
  // AND selection state. rfNodes 从 store 重建时不带 `selected`,若不在这里保留,
  // 任何 store 更新(如在右侧属性面板编辑字段触发的 updateNode)都会让 RF 丢掉选中
  // → onSelectionChange([]) → 属性面板退回空态(面板"消失",无法持续编辑)。保留
  // `selected`/`dragging` 让选中跨 store 更新稳定,面板可固定编辑。
  useEffect(() => {
    setNodes((prev) =>
      rfNodes.map((rfn) => {
        const existing = prev.find((p) => p.id === rfn.id)
        if (!existing) return rfn
        // Preserve React Flow's resize state (width/height/style set by NodeResizer)
        // + 交互态(selected/dragging),否则属性面板编辑会清掉选中。
        return {
          ...rfn,
          ...(existing.width != null ? { width: existing.width } : {}),
          ...(existing.height != null ? { height: existing.height } : {}),
          ...(existing.style ? { style: existing.style } : {}),
          selected: existing.selected,
          ...(existing.dragging != null ? { dragging: existing.dragging } : {}),
        }
      }),
    )
  }, [rfNodes, setNodes])
  // round5:跟节点同理(#237 修过节点、边漏了)——`setEdges(rfEdges)` 全量覆盖会把
  // 用户单击高亮的边(onEdgeClick 写的 selected/accent style)在任意 store 更新(改字段
  // → updateNode → rfEdges 重算)后冲回 muted 默认,边高亮「闪一下就没」。保留 selected/style。
  useEffect(() => {
    setEdges((prev) =>
      rfEdges.map((rfe) => {
        const existing = prev.find((p) => p.id === rfe.id)
        if (!existing) return rfe
        return { ...rfe, selected: existing.selected, ...(existing.style ? { style: existing.style } : {}) }
      }),
    )
  }, [rfEdges, setEdges])

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      onNodesChangeInternal(changes)
      for (const change of changes) {
        if (change.type === 'remove') storeRemoveNode(change.id)
      }
    },
    [onNodesChangeInternal, storeRemoveNode],
  )

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      onEdgesChangeInternal(changes)
      for (const change of changes) {
        if (change.type === 'remove') storeRemoveEdge(change.id)
      }
    },
    [onEdgesChangeInternal, storeRemoveEdge],
  )

  const onConnect = useCallback(
    (params: Connection) => {
      setEdges((eds) => addEdge({ ...params, type: 'portTyped' }, eds))
      const edgeId = crypto.randomUUID().slice(0, 8)
      storeAddEdge({
        id: edgeId,
        source: params.source,
        sourceHandle: params.sourceHandle ?? '',
        target: params.target,
        targetHandle: params.targetHandle ?? '',
      })
    },
    [setEdges, storeAddEdge],
  )

  // 在落点建一个与起点端口连好的节点(参数化,供「菜单选中」和「拖出输出口自动建终端」复用)。
  const spawnConnectedNode = useCallback((
    type: NodeType, portType: PortType,
    fromNodeId: string, fromHandle: string | null, handleType: 'source' | 'target',
    flowX: number, flowY: number,
  ) => {
    const id = crypto.randomUUID().slice(0, 8)
    // source 拖出 → 新节点放落点;target 拖出(从输入口) → 左移一个节点宽,让输出口对齐落点。
    const position = { x: handleType === 'source' ? flowX : flowX - 320, y: flowY - 20 }
    const newNode: WorkflowNode = { id, type, position, data: {} }
    let edge: WorkflowEdge | null = null
    if (handleType === 'source') {
      const th = firstInputHandle(type, portType)
      if (th) edge = { id: crypto.randomUUID().slice(0, 8), source: fromNodeId, sourceHandle: fromHandle ?? '', target: id, targetHandle: th }
    } else {
      const sh = firstOutputHandle(type, portType)
      if (sh) edge = { id: crypto.randomUUID().slice(0, 8), source: id, sourceHandle: sh, target: fromNodeId, targetHandle: fromHandle ?? '' }
    }
    storeAddNodesWithEdges([newNode], edge ? [edge] : [])
    setNodes((nds) => [
      ...nds.map((n) => ({ ...n, selected: false })),
      { id, type, position, data: {}, style: { width: 320 }, selected: true } as Node,
    ])
    if (edge) setEdges((eds) => addEdge({ id: edge!.id, source: edge!.source, sourceHandle: edge!.sourceHandle, target: edge!.target, targetHandle: edge!.targetHandle, type: 'portTyped' }, eds))
  }, [setNodes, setEdges, storeAddNodesWithEdges])

  // 端口拖到空白建节点(借鉴 Infinite-Canvas):记起点 → 落 pane 上则弹兼容节点菜单。
  const onConnectStart = useCallback(
    (_e: unknown, params: { nodeId: string | null; handleId: string | null; handleType: 'source' | 'target' | null }) => {
      connectStartRef.current = params.nodeId && params.handleType
        ? { nodeId: params.nodeId, handleId: params.handleId, handleType: params.handleType }
        : null
    },
    [],
  )

  const onConnectEnd = useCallback((event: MouseEvent | TouchEvent) => {
    const start = connectStartRef.current
    connectStartRef.current = null
    if (!start) return
    // 只在落到空白画布(pane)时弹菜单;落到 handle 由 onConnect 正常连。
    const tgt = event.target as HTMLElement | null
    if (!tgt?.classList?.contains('react-flow__pane')) return
    const rfi = reactFlowInstance.current
    const bounds = reactFlowWrapper.current?.getBoundingClientRect()
    if (!rfi || !bounds) return
    const clientX = 'clientX' in event ? event.clientX : event.changedTouches?.[0]?.clientX
    const clientY = 'clientY' in event ? event.clientY : event.changedTouches?.[0]?.clientY
    if (clientX == null || clientY == null) return
    const fromNode = nodesRef.current.find((n) => n.id === start.nodeId)
    const portType = getPortType(fromNode?.type ?? '', start.handleId)
    if (!portType) return
    const flow = rfi.screenToFlowPosition({ x: clientX, y: clientY })
    // 从**输出口**拖到空白 + 该类型有专属终端输出节点 → 直接建它+连好(免菜单,
    // 借鉴 Infinite-Canvas「从生成器拖出自动建 Output」)。image→图像输出 / audio→输出播放 /
    // text→文本输出。其余情况(输入口拖出、或无终端节点)仍走兼容节点菜单。
    const TERMINAL_OUTPUT: Partial<Record<PortType, NodeType>> = {
      image: 'image_output', audio: 'output', text: 'text_output',
    }
    if (start.handleType === 'source' && TERMINAL_OUTPUT[portType] && NODE_DEFS[TERMINAL_OUTPUT[portType]!]) {
      spawnConnectedNode(TERMINAL_OUTPUT[portType]!, portType, start.nodeId, start.handleId, 'source', flow.x, flow.y)
      return
    }
    const choices = start.handleType === 'source'
      ? choicesAcceptingInput(portType)
      : choicesProvidingOutput(portType)
    if (choices.length === 0) return
    setCreateMenu({
      x: clientX - bounds.left, y: clientY - bounds.top,
      flowX: flow.x, flowY: flow.y,
      fromNodeId: start.nodeId, fromHandle: start.handleId, handleType: start.handleType,
      portType, choices,
    })
  }, [spawnConnectedNode])

  // 菜单选中 → 用 spawnConnectedNode 在落点建节点并连好(单次 undo);关菜单。
  const createConnectedNode = useCallback((type: NodeType) => {
    const m = createMenu
    if (!m) return
    spawnConnectedNode(type, m.portType, m.fromNodeId, m.fromHandle, m.handleType, m.flowX, m.flowY)
    setCreateMenu(null)
  }, [createMenu, spawnConnectedNode])

  // 画布右键菜单选中 → 在光标 flow 坐标处建节点(无连接)。
  const createNodeAt = useCallback((type: NodeType, flowX: number, flowY: number) => {
    const id = crypto.randomUUID().slice(0, 8)
    const position = { x: flowX, y: flowY }
    setNodes((nds) => [
      ...nds.map((n) => ({ ...n, selected: false })),
      { id, type, position, data: {}, style: { width: 320 }, selected: true } as Node,
    ])
    storeAddNode({ id, type, position, data: {} })
    setPaneMenu(null)
  }, [setNodes, storeAddNode])

  // 节点库双击 → 在画布视口中心建该节点(NodeLibraryPanel 派发 'nodelib-add-node')。
  useEffect(() => {
    const onAdd = (e: Event) => {
      const type = (e as CustomEvent).detail?.type as NodeType
      if (!type || !NODE_DEFS[type]) return
      const rfi = reactFlowInstance.current
      const bounds = reactFlowWrapper.current?.getBoundingClientRect()
      if (!rfi || !bounds) return
      const center = rfi.screenToFlowPosition({ x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 })
      createNodeAt(type, center.x, center.y)
    }
    window.addEventListener('nodelib-add-node', onAdd)
    return () => window.removeEventListener('nodelib-add-node', onAdd)
  }, [createNodeAt])

  // 剪贴板粘贴图片(对齐 Infinite-Canvas):非编辑态 Ctrl+V 含图 → 选中多模态节点则追加,
  // 否则在视口中心新建多模态输入节点。in-app 节点剪贴板由 keydown 处理,二者互不干扰
  // (图片走 clipboardData.files,节点走内存 clipboardRef)。
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const tgt = e.target as HTMLElement | null
      if (tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || tgt.isContentEditable)) return
      const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith('image/'))
      if (!file) return
      e.preventDefault()
      const reader = new FileReader()
      reader.onload = () => {
        const url = reader.result as string
        if (!url) return
        const mm = nodesRef.current.find((n) => n.selected && n.type === 'multimodal_input')
        if (mm) {
          const cur = ((mm.data as Record<string, unknown>)?.images as string[]) || []
          const next = [...cur, url]
          updateNode(mm.id, { images: next, image: next[0] })
          return
        }
        const rfi = reactFlowInstance.current
        const bounds = reactFlowWrapper.current?.getBoundingClientRect()
        const center = rfi && bounds
          ? rfi.screenToFlowPosition({ x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 2 })
          : { x: 0, y: 0 }
        const id = crypto.randomUUID().slice(0, 8)
        const data = { images: [url], image: url }
        storeAddNode({ id, type: 'multimodal_input', position: center, data })
        setNodes((nds) => [
          ...nds.map((n) => ({ ...n, selected: false })),
          { id, type: 'multimodal_input', position: center, data, style: { width: 320 }, selected: true } as Node,
        ])
      }
      reader.readAsDataURL(file)
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [updateNode, storeAddNode, setNodes])

  const isValidConnection = useCallback(
    (connection: Edge | Connection) => {
      // round5:挡自连(自环)——拖到节点自己的同类型输入会建自环边,要等执行时
      // topoSort 才报「循环依赖」;backend 执行路径前端更无守卫。提前挡掉。
      if (connection.source === connection.target) return false
      const currentNodes = nodesRef.current
      const sourceNode = currentNodes.find((n) => n.id === connection.source)
      const targetNode = currentNodes.find((n) => n.id === connection.target)
      if (!sourceNode || !targetNode) return false
      const sourceType = getPortType(sourceNode.type ?? '', connection.sourceHandle)
      const targetType = getPortType(targetNode.type ?? '', connection.targetHandle)
      if (!sourceType || !targetType) return false
      return sourceType === targetType
    },
    [],
  )

  // Sync React Flow positions/sizes to Zustand store (uses ref to avoid stale closures)
  const syncToStore = useCallback(() => {
    const currentNodes = nodesRef.current
    setWorkflow({
      ...workflow,
      nodes: workflow.nodes.map((wn) => {
        const rfNode = currentNodes.find((n) => n.id === wn.id)
        if (!rfNode) return wn
        const updated: any = { ...wn, position: rfNode.position }
        if (rfNode.style) updated.style = rfNode.style
        if (rfNode.width != null) updated.width = rfNode.width
        if (rfNode.height != null) updated.height = rfNode.height
        return updated
      }),
    })
  }, [workflow, setWorkflow])

  // Sync resize changes to store
  useEffect(() => {
    const handler = () => syncToStore()
    window.addEventListener('node-resize-end', handler)
    return () => window.removeEventListener('node-resize-end', handler)
  }, [syncToStore])

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
  }, [])

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      const type = event.dataTransfer.getData('application/reactflow') as NodeType
      if (!type || !NODE_DEFS[type]) return
      const rfi = reactFlowInstance.current
      if (!rfi || !reactFlowWrapper.current) return
      const bounds = reactFlowWrapper.current.getBoundingClientRect()
      const position = rfi.screenToFlowPosition({
        x: event.clientX - bounds.left,
        y: event.clientY - bounds.top,
      })
      const id = crypto.randomUUID().slice(0, 8)

      // 拖节点到连线上 → 自动插入(借鉴 ComfyUI/tldraw)。落点命中某条边、且该节点有
      // 与边端口类型匹配的输入口+输出口(pass-through 类如提示模板)→ 删原边、串 source→新→target。
      const els = document.elementsFromPoint(event.clientX, event.clientY)
      let hitEdgeId: string | null = null
      for (const el of els) {
        const g = (el as HTMLElement).closest?.('.react-flow__edge')
        if (g) { hitEdgeId = g.getAttribute('data-id'); break }
      }
      const targetEdge = hitEdgeId ? workflow.edges.find((e) => e.id === hitEdgeId) : null
      if (targetEdge) {
        const portType = getPortType(nodeTypeById[targetEdge.source] ?? '', targetEdge.sourceHandle)
        const inH = portType ? firstInputHandle(type, portType) : undefined
        const outH = portType ? firstOutputHandle(type, portType) : undefined
        if (portType && inH && outH) {
          const e1: WorkflowEdge = { id: crypto.randomUUID().slice(0, 8), source: targetEdge.source, sourceHandle: targetEdge.sourceHandle, target: id, targetHandle: inH }
          const e2: WorkflowEdge = { id: crypto.randomUUID().slice(0, 8), source: id, sourceHandle: outH, target: targetEdge.target, targetHandle: targetEdge.targetHandle }
          storeSpliceNodeOnEdge({ id, type, position, data: {} }, targetEdge.id, [e1, e2])
          setEdges((eds) => [
            ...eds.filter((e) => e.id !== targetEdge.id),
            ...[e1, e2].map((e) => ({ id: e.id, source: e.source, sourceHandle: e.sourceHandle, target: e.target, targetHandle: e.targetHandle, type: 'portTyped' } as Edge)),
          ])
          setNodes((nds) => [
            ...nds.map((n) => ({ ...n, selected: false })),
            { id, type, position, data: {}, style: { width: 320 }, selected: true } as Node,
          ])
          return
        }
      }

      const newNode: Node = { id, type, position, data: {}, style: { width: 320 } }
      setNodes((nds) => [...nds, newNode])
      storeAddNode({ id, type, position, data: {} })
    },
    [setNodes, setEdges, storeAddNode, storeSpliceNodeOnEdge, workflow.edges, nodeTypeById],
  )

  // 复制选中节点(+ 它们之间的内部连线)到剪贴板。深拷贝 data 防共享引用;
  // 保留原 id 以便粘贴时按 id 映射重连内部边。
  const copySelection = useCallback(() => {
    const selected = nodesRef.current.filter((n) => n.selected)
    if (selected.length === 0) return false
    const selectedIds = new Set(selected.map((n) => n.id))
    clipboardRef.current = {
      nodes: selected.map((n) => ({
        id: n.id,
        type: n.type ?? '',
        data: structuredClone(n.data ?? {}) as Record<string, unknown>,
        position: { ...n.position },
        style: (n as any).style,
        width: (n as any).width,
        height: (n as any).height,
      })),
      // 只带「两端都在选区内」的边 —— 跨选区边粘贴后无对应端点。
      edges: edgesRef.current
        .filter((e) => selectedIds.has(e.source) && selectedIds.has(e.target))
        .map((e) => ({
          source: e.source,
          sourceHandle: e.sourceHandle ?? '',
          target: e.target,
          targetHandle: e.targetHandle ?? '',
        })),
    }
    pasteSeqRef.current = 0
    return true
  }, [])

  // 粘贴:用纯函数 buildPastedGraph 发新 id、偏移落位、内部边重连(逻辑在
  // utils/pasteGraph.ts,有单测)。走 store 批量 addNodesWithEdges(单次 undo),
  // 并即时 setNodes/setEdges 选中新节点。
  const pasteClipboard = useCallback(() => {
    const clip = clipboardRef.current
    if (!clip || clip.nodes.length === 0) return
    pasteSeqRef.current += 1
    const { nodes: pn, edges: pe } = buildPastedGraph(clip, 40 * pasteSeqRef.current, () =>
      crypto.randomUUID().slice(0, 8),
    )
    const newNodes = pn.map((n) => {
      const node: any = { id: n.id, type: n.type, position: n.position, data: n.data, style: n.style ?? { width: 320 } }
      if (n.width != null) node.width = n.width
      if (n.height != null) node.height = n.height
      return node as WorkflowNode
    })
    const newEdges: WorkflowEdge[] = pe.map((e) => ({
      id: e.id, source: e.source, sourceHandle: e.sourceHandle, target: e.target, targetHandle: e.targetHandle,
    }))

    storeAddNodesWithEdges(newNodes, newEdges)
    // 即时渲染 + 选中粘贴出来的节点(取消原选区),方便接着拖动。
    setNodes((nds) => [
      ...nds.map((n) => ({ ...n, selected: false })),
      ...newNodes.map((n) => ({
        id: n.id,
        type: n.type,
        position: n.position,
        data: n.data,
        style: (n as any).style ?? { width: 320 },
        selected: true,
      } as Node)),
    ])
    setEdges((eds) => [
      ...eds,
      ...newEdges.map((e) => ({ id: e.id, source: e.source, sourceHandle: e.sourceHandle, target: e.target, targetHandle: e.targetHandle, type: 'portTyped' } as Edge)),
    ])
  }, [setNodes, setEdges, storeAddNodesWithEdges])

  // Alt+拖拽复制(对齐 Infinite-Canvas duplicateForAltDrag):拖起时按住 Alt,在原位
  // 留一份副本(offset 0),原节点继续被拖走 → 等效「拖出一个副本」。多选时整组复制
  // (含组内连线)。复用纯函数 buildPastedGraph(发新 id / 内部边重连)。
  const altDuplicate = useCallback((dragNodeId: string) => {
    const all = nodesRef.current
    const sel = all.filter((n) => n.selected)
    const set = sel.some((n) => n.id === dragNodeId) && sel.length ? sel : all.filter((n) => n.id === dragNodeId)
    if (!set.length) return
    const ids = new Set(set.map((n) => n.id))
    const clip = {
      nodes: set.map((n) => ({
        id: n.id, type: n.type ?? '', data: structuredClone(n.data ?? {}) as Record<string, unknown>,
        position: { ...n.position }, style: (n as any).style, width: (n as any).width, height: (n as any).height,
      })),
      edges: edgesRef.current
        .filter((e) => ids.has(e.source) && ids.has(e.target))
        .map((e) => ({ source: e.source, sourceHandle: e.sourceHandle ?? '', target: e.target, targetHandle: e.targetHandle ?? '' })),
    }
    const { nodes: pn, edges: pe } = buildPastedGraph(clip, 0, () => crypto.randomUUID().slice(0, 8))
    const newNodes = pn.map((n) => {
      const node: any = { id: n.id, type: n.type, position: n.position, data: n.data, style: n.style ?? { width: 320 } }
      if (n.width != null) node.width = n.width
      if (n.height != null) node.height = n.height
      return node as WorkflowNode
    })
    const newEdges: WorkflowEdge[] = pe.map((e) => ({ id: e.id, source: e.source, sourceHandle: e.sourceHandle, target: e.target, targetHandle: e.targetHandle }))
    storeAddNodesWithEdges(newNodes, newEdges)
    // 副本插到原位且不选中(原节点保持选中 + 继续被拖)。
    setNodes((nds) => [
      ...newNodes.map((n) => ({ id: n.id, type: n.type, position: n.position, data: n.data, style: (n as any).style ?? { width: 320 }, selected: false } as Node)),
      ...nds,
    ])
    if (newEdges.length) {
      setEdges((eds) => [...eds, ...newEdges.map((e) => ({ id: e.id, source: e.source, sourceHandle: e.sourceHandle, target: e.target, targetHandle: e.targetHandle, type: 'portTyped' } as Edge))])
    }
  }, [setNodes, setEdges, storeAddNodesWithEdges])

  // 选中节点打包成分组框(对齐 ComfyUI/Infinite-Canvas Ctrl+G)。写显式 nodeIds,
  // 初始 x/y/w/h 用成员实测包围盒(measured);渲染时 GroupLayer 会按成员实测再自适应。
  // 供 Ctrl+G 和右键菜单复用。
  const groupSelected = useCallback(() => {
    const selected = nodesRef.current.filter((n) => n.selected)
    if (selected.length === 0) return
    const members = selected.map((n) => ({
      x: n.position.x,
      y: n.position.y,
      width: (n.measured?.width ?? (n as any).width ?? (n.style as any)?.width ?? 320) as number,
      height: (n.measured?.height ?? (n as any).height ?? 160) as number,
    }))
    const b = computeGroupBounds(members)
    if (!b) return
    const palette = ['#a855f7', '#3b82f6', '#22c55e', '#f59e0b', '#ec4899']
    const idx = (workflow.groups?.length ?? 0) % palette.length
    storeAddGroup({
      id: crypto.randomUUID().slice(0, 8),
      title: '分组',
      x: b.x, y: b.y, width: b.width, height: b.height,
      color: palette[idx],
      nodeIds: selected.map((n) => n.id),
    })
  }, [workflow.groups, storeAddGroup])

  // 解组(Ctrl+Shift+G):删掉「含任一选中节点」的分组(不删节点);无选中则删最近建的分组。
  const ungroupSelected = useCallback(() => {
    const selectedIds = new Set(nodesRef.current.filter((n) => n.selected).map((n) => n.id))
    const groups = workflow.groups ?? []
    if (groups.length === 0) return
    const targets = selectedIds.size
      ? groups.filter((g) => (g.nodeIds ?? []).some((id) => selectedIds.has(id)))
      : groups.slice(-1)
    for (const g of targets) storeRemoveGroup(g.id)
  }, [workflow.groups, storeRemoveGroup])

  // 复制 Ctrl/Cmd+C / 粘贴 Ctrl/Cmd+V / 原地复制 Ctrl/Cmd+D。
  // 与 undo/redo 同样:focus 在 input/textarea/contenteditable 时放行原生行为。
  // Ctrl+V 仅在 in-app 剪贴板有节点时接管 —— 否则放行(让 MultimodalInputNode
  // 的图片粘贴等原生 paste 正常工作)。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      if (!mod) return
      const tgt = e.target as HTMLElement | null
      if (tgt) {
        const tag = tgt.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tgt.isContentEditable) return
      }
      const k = e.key.toLowerCase()
      if (k === 'c') {
        copySelection()
      } else if (k === 'v') {
        if (clipboardRef.current && clipboardRef.current.nodes.length > 0) {
          e.preventDefault()
          pasteClipboard()
        }
      } else if (k === 'd') {
        if (nodesRef.current.some((n) => n.selected)) {
          e.preventDefault()
          copySelection()
          pasteClipboard()
        }
      } else if (k === 'b') {
        // 旁路/取消旁路选中节点(对齐 ComfyUI Ctrl+B)。整组按「是否全已旁路」翻转:
        // 有任一未旁路 → 全部旁路;否则全部取消旁路。flag 落 node.data.bypassed。
        const selected = nodesRef.current.filter((n) => n.selected)
        if (selected.length > 0) {
          e.preventDefault()
          const anyOn = selected.some((n) => !(n.data as any)?.bypassed)
          for (const n of selected) updateNode(n.id, { bypassed: anyOn })
        }
      } else if (k === 'g') {
        // Ctrl+G 成组 / Ctrl+Shift+G 解组(对齐 Infinite-Canvas)。
        e.preventDefault()
        if (e.shiftKey) ungroupSelected()
        else if (nodesRef.current.some((n) => n.selected)) groupSelected()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [copySelection, pasteClipboard, updateNode, groupSelected, ungroupSelected])

  // Z(无修饰键)→ 缩放到全部节点总览(对齐 Infinite-Canvas Z 总览)。编辑态/带修饰键放行。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return
      if (e.key.toLowerCase() !== 'z') return
      const tgt = e.target as HTMLElement | null
      if (tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || tgt.isContentEditable)) return
      e.preventDefault()
      reactFlowInstance.current?.fitView({ padding: 0.2, duration: 300 })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // m09: 画布模式下左节点库 + 右属性面板**常驻**。overlay 视图
  // （dashboard / services / 设置 等）下两边都隐藏。
  // v3 没有 panel 切换 — PanelStore.PANEL_ITEMS 已空 — activePanel
  // 系统作为 legacy workflows/presets 兜底（如有，仍然 PANEL_MAP 渲染）。
  const showPropertyPanel = !activeOverlay
  const PROPERTY_PANEL_WIDTH = 300

  const LegacyPanel = activePanel && activePanel !== 'nodes' ? PANEL_MAP[activePanel] : null
  const showLegacyPanel = !!LegacyPanel && !activeOverlay
  const showNodeLibrary = !activeOverlay && !showLegacyPanel
  const NodeLibraryComponent = PANEL_MAP.nodes

  return (
    <div className="relative flex-1 overflow-hidden" ref={reactFlowWrapper}>
      {/* Floating side panel — m09: 常驻节点库；legacy workflows/presets 仍兼容 */}
      {showNodeLibrary && <NodeLibraryComponent />}
      {showLegacyPanel && LegacyPanel && <LegacyPanel />}

      {/* Canvas area — offset when panel is open */}
      <div
        data-testid="workflow-canvas"
        className="absolute inset-0 transition-[left] duration-200"
        style={{
          left: showNodeLibrary || showLegacyPanel ? panelWidth : 0,
          right: showPropertyPanel ? PROPERTY_PANEL_WIDTH : 0,
        }}
        onDoubleClick={(e) => {
          // 双击画布空白 → 光标处建节点菜单(对齐 Infinite-Canvas 双击建节点)。
          const tgt = e.target as HTMLElement | null
          if (!tgt?.classList?.contains('react-flow__pane')) return
          const rfi = reactFlowInstance.current
          const bounds = reactFlowWrapper.current?.getBoundingClientRect()
          if (!rfi || !bounds) return
          const flow = rfi.screenToFlowPosition({ x: e.clientX, y: e.clientY })
          setCtxMenu(null)
          setPaneMenu({ x: e.clientX - bounds.left, y: e.clientY - bounds.top, flowX: flow.x, flowY: flow.y })
        }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onConnectStart={onConnectStart}
          onConnectEnd={onConnectEnd}
          isValidConnection={isValidConnection}
          onInit={(instance) => { reactFlowInstance.current = instance }}
          onDragOver={onDragOver}
          onDrop={onDrop}
          onSelectionChange={({ nodes: sel }) => {
            // Only single-select drives the property panel; multi-select / deselect → null.
            setSelectedNodeId(sel.length === 1 ? sel[0].id : null)
          }}
          onNodeDragStart={(event, node) => {
            // Alt 拖拽 → 原位留副本(对齐 Infinite-Canvas)。
            if ((event as unknown as MouseEvent).altKey) altDuplicate(node.id)
          }}
          onNodeDragStop={syncToStore}
          onEdgeDoubleClick={(_event, edge) => {
            setEdges((eds) => eds.filter((e) => e.id !== edge.id))
            storeRemoveEdge(edge.id)
          }}
          onEdgeContextMenu={(event, edge) => {
            event.preventDefault()
            setEdges((eds) => eds.filter((e) => e.id !== edge.id))
            storeRemoveEdge(edge.id)
          }}
          onNodeContextMenu={(event, node) => {
            event.preventDefault()
            // 右键:若该节点已在多选内,保留整个选区(分组/复制作用于整组);否则单选它。
            setNodes((nds) => {
              const alreadyInSel = nds.find((n) => n.id === node.id)?.selected && nds.filter((n) => n.selected).length > 1
              return alreadyInSel ? nds : nds.map((n) => ({ ...n, selected: n.id === node.id }))
            })
            const bounds = reactFlowWrapper.current?.getBoundingClientRect()
            setCtxMenu({
              x: event.clientX - (bounds?.left ?? 0),
              y: event.clientY - (bounds?.top ?? 0),
              nodeId: node.id,
            })
          }}
          onPaneClick={() => { setCtxMenu(null); setPaneMenu(null) }}
          onMoveStart={() => { setCtxMenu(null); setPaneMenu(null) }}
          onPaneContextMenu={(event) => {
            event.preventDefault()
            const rfi = reactFlowInstance.current
            const bounds = reactFlowWrapper.current?.getBoundingClientRect()
            if (!rfi || !bounds) return
            const me = event as unknown as MouseEvent
            const flow = rfi.screenToFlowPosition({ x: me.clientX, y: me.clientY })
            setCtxMenu(null)
            setPaneMenu({ x: me.clientX - bounds.left, y: me.clientY - bounds.top, flowX: flow.x, flowY: flow.y })
          }}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          deleteKeyCode={['Backspace', 'Delete']}
          // 框选(对齐 Infinite-Canvas「拖拽画布移动,Ctrl 框选多选」):左键拖默认平移;
          // 按住 Ctrl/Cmd 拖 → 拉框选;Partial = 相交即选(AABB);多选拖动 RF 原生支持。
          selectionKeyCode={['Control', 'Meta']}
          multiSelectionKeyCode={['Control', 'Meta']}
          selectionMode={SelectionMode.Partial}
          // 关掉 RF 原生双击缩放:双击空白改为弹建节点菜单(否则双击只放大,且缩放的
          // onMoveStart 会把刚弹的菜单关掉)。真机巡检发现。
          zoomOnDoubleClick={false}
          edgesReconnectable
          minZoom={0.05}
          maxZoom={8}
          fitView
          fitViewOptions={{ padding: 0.1, minZoom: 0.5, maxZoom: 1.5 }}
          defaultEdgeOptions={{
            type: 'portTyped',
            focusable: true,
            interactionWidth: 20,
          }}
          style={{ background: 'var(--bg)' }}
        >
          {/* Infinite-Canvas 风 dotted 网格(点色随主题 --grid)。 */}
          <Background variant={BackgroundVariant.Dots} color="var(--grid)" gap={24} size={1.4} />
          <GroupLayer />
          <Controls />
          <MiniMap
            nodeColor={() => 'var(--muted-strong)'}
            style={{ background: 'var(--bg-accent)' }}
            pannable
            zoomable
          />
        </ReactFlow>

        {/* 底部操作提示条(对齐 Infinite-Canvas) */}
        <div
          style={{
            position: 'absolute', bottom: 10, left: '50%', transform: 'translateX(-50%)',
            pointerEvents: 'none', userSelect: 'none', zIndex: 5,
            fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap',
            padding: '4px 12px', borderRadius: 999,
            background: 'var(--card-hl)', border: '1px solid var(--border)',
            backdropFilter: 'blur(6px)',
          }}
        >
          拖拽画布移动,Ctrl 框选多选,拖动选中节点可一起移动
        </div>

        {/* 快捷键帮助面板(? 浮层) */}
        <ShortcutsHelp />

        {/* 全屏图片预览(跨图 ←/→ 切换) */}
        <Lightbox />
      </div>

      {/* 节点右键菜单(旁路/复制/删除)*/}
      {ctxMenu && (() => {
        const ctxNode = workflow.nodes.find((n) => n.id === ctxMenu.nodeId)
        const isBypassed = !!ctxNode?.data?.bypassed
        const close = () => setCtxMenu(null)
        const item = (icon: React.ReactNode, label: string, onClick: () => void, danger?: boolean) => (
          <button
            type="button"
            onClick={() => { onClick(); close() }}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left"
            style={{ color: danger ? 'var(--error, #ef4444)' : 'var(--text)', background: 'transparent', border: 'none', cursor: 'pointer' }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-hover)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
          >
            {icon}
            <span>{label}</span>
          </button>
        )
        return (
          <>
            {/* 点击空白关闭(覆盖全画布的透明层)*/}
            <div className="absolute inset-0 z-30" onClick={close} onContextMenu={(e) => { e.preventDefault(); close() }} />
            <div
              className="absolute z-40 py-1 rounded-md"
              style={{
                top: ctxMenu.y, left: ctxMenu.x, minWidth: 140,
                background: 'var(--bg-elevated)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-md)',
              }}
            >
              {item(<Pencil size={13} />, '重命名', () => {
                // 触发该节点 BaseNode 的就地编辑(BaseNode 监听 'node-rename')。
                setTimeout(() => window.dispatchEvent(new CustomEvent('node-rename', { detail: { id: ctxMenu.nodeId } })), 0)
              })}
              {(() => {
                const d = ctxNode?.data as Record<string, unknown> | undefined
                const url = (d?.image_url || d?.image || d?.image_a_url || d?.image_b_url) as string | undefined
                if (!url) return null
                return item(<Download size={13} />, '下载图', () => {
                  const a = document.createElement('a')
                  a.href = url
                  a.download = `${ctxMenu.nodeId}.png`
                  document.body.appendChild(a); a.click(); a.remove()
                })
              })()}
              {item(<Ban size={13} />, isBypassed ? '取消旁路' : '旁路 (Ctrl+B)', () => updateNode(ctxMenu.nodeId, { bypassed: !isBypassed }))}
              {item(<Copy size={13} />, '复制 (Ctrl+D)', () => {
                // 延后一拍:选中态(右键已设)在下一帧稳定后再 copy+paste
                setTimeout(() => { copySelection(); pasteClipboard() }, 0)
              })}
              {item(<Group size={13} />, '分组 (Ctrl+G)', () => { setTimeout(() => groupSelected(), 0) })}
              {item(<Ungroup size={13} />, '解组 (Ctrl+Shift+G)', () => { setTimeout(() => ungroupSelected(), 0) })}
              {item(<Trash2 size={13} />, '删除', () => {
                setNodes((nds) => nds.filter((n) => n.id !== ctxMenu.nodeId))
                storeRemoveNode(ctxMenu.nodeId)
              }, true)}
            </div>
          </>
        )
      })()}

      {/* 端口拖到空白 → 快捷建相连节点菜单 */}
      {createMenu && (
        <NodeCreateMenu
          x={createMenu.x}
          y={createMenu.y}
          choices={createMenu.choices}
          title={createMenu.handleType === 'source' ? '连接到…' : '从…连入'}
          onPick={(type) => createConnectedNode(type)}
          onClose={() => setCreateMenu(null)}
        />
      )}

      {/* 画布右键 → 光标处快捷建节点菜单 */}
      {paneMenu && (
        <NodeCreateMenu
          x={paneMenu.x}
          y={paneMenu.y}
          choices={getAllChoices()}
          title="添加节点"
          onPick={(type) => createNodeAt(type, paneMenu.flowX, paneMenu.flowY)}
          onClose={() => setPaneMenu(null)}
        />
      )}

      {/* m09: 节点属性面板（画布模式常驻右侧；overlay 视图下隐藏） */}
      {showPropertyPanel && <NodePropertyPanel />}

      {/* Page overlays —— 懒加载,首次打开时按需 fetch chunk。fallback 必须是**不透明满屏**的
          OverlayLoading:overlay 本身盖在画布上面,fallback 给 null 不是「什么都不显示」,
          而是在 chunk 到达前把底下的画布整个露出来 —— 刷新 /services /settings 闪一帧画布
          的老 bug 就是这么来的。activeOverlay 为 null 时没有 lazy 子节点会挂起,fallback
          不会出现,画布路由照旧直接渲染。 */}
      <Suspense fallback={<OverlayLoading />}>
        {activeOverlay === 'dashboard' && <DashboardOverlay />}
        {activeOverlay === 'models' && <ModelsOverlay />}
        {activeOverlay === 'settings' && <SettingsOverlay />}
        {activeOverlay === 'preset-detail' && <PresetDetailOverlay />}
        {activeOverlay === 'api-keys-list' && <ApiKeysList />}
        {activeOverlay === 'api-key-detail' && <ApiKeyDetail />}
        {activeOverlay === 'agents' && <AgentManagementOverlay />}
        {activeOverlay === 'logs' && <LogsOverlay />}
        {activeOverlay === 'node-packages' && <NodePackagesOverlay />}
        {activeOverlay === 'services' && <ServicesList />}
        {activeOverlay === 'apps' && <ServicesList />}
        {activeOverlay === 'service-detail' && <ServiceDetailRoute />}
        {activeOverlay === 'workflows-list' && <WorkflowsList />}
        {activeOverlay === 'usage' && <UsagePage />}
        {activeOverlay === 'status' && <StatusOverlay />}
        {activeOverlay === 'history' && <HistoryOverlay />}
      </Suspense>

      {/* PR-3g(2026-05-28 任务面板重置收尾):删 QueueProgressOverlay 老画布右上浮窗
          (PR-170 时代方案)。任务进度统一由 GlobalTopbar 任务下拉 + Active/History
          dropdown panel 承担(PR-3a-3e)。旧 overlay 文件留着只为兼容 TaskPanel 单测,
          后续如果完全摘除可再删 QueueProgressOverlay.tsx + TaskMenuButton.tsx。 */}
    </div>
  )
}
