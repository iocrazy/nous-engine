import { NODE_DEFS, type PortDef, type NodeType } from './workflow'

export type WidgetType = 'input' | 'textarea' | 'select' | 'slider' | 'checkbox' | 'agent_select' | 'model_select' | 'image_upload'

export interface WidgetDef {
  name: string
  label: string
  widget: WidgetType
  // 兼容三种写法:对象 [{value,label,description?,color?}] 或纯字符串 ['a','b'](plugin node.yaml 常用后者)。
  // description = 副标题(NodeSelectPopover 显示);color = 左侧彩色点(paperclip 风格)。
  options?: (string | { value: string; label: string; description?: string; color?: string })[]
  min?: number
  max?: number
  step?: number
  precision?: number
  rows?: number
  default?: unknown
  filter?: string
}

export interface DeclarativeNodeDef {
  type: NodeType
  label: string
  category: string
  badge: string
  badgeColor: string
  widgets: WidgetDef[]
}

export const DECLARATIVE_NODES: Record<string, DeclarativeNodeDef> = {
  llm: {
    type: 'llm',
    label: 'LLM',
    category: 'ai',
    badge: 'AI',
    badgeColor: 'var(--purple)',
    widgets: [
      { name: 'system', label: '系统提示', widget: 'textarea', rows: 3 },
      { name: 'model_key', label: '模型', widget: 'model_select', filter: 'llm' },
      { name: 'temperature', label: '温度', widget: 'slider', min: 0, max: 2, step: 0.1, precision: 1, default: 0.7 },
      { name: 'max_tokens', label: '最大 Token', widget: 'slider', min: 1, max: 262144, step: 1, precision: 0, default: 4096 },
      { name: 'enable_thinking', label: '思维模式', widget: 'select', options: [
        { value: 'true', label: '开启' },
        { value: 'false', label: '关闭' },
      ], default: 'false' },
      { name: 'stream', label: '流式输出', widget: 'select', options: [
        { value: 'true', label: '开启' },
        { value: 'false', label: '关闭' },
      ], default: 'true' },
    ],
  },
  prompt_template: {
    type: 'prompt_template',
    label: '提示模板',
    category: 'ai',
    badge: 'AI',
    badgeColor: 'var(--purple)',
    widgets: [
      { name: 'template', label: '模板', widget: 'textarea', rows: 5 },
    ],
  },
  agent: {
    type: 'agent',
    label: 'Agent',
    category: 'ai',
    badge: 'AI',
    badgeColor: 'var(--purple)',
    widgets: [
      { name: 'agent_name', label: 'Agent', widget: 'agent_select' },
    ],
  },
  if_else: {
    type: 'if_else',
    label: '条件分支',
    category: 'control',
    badge: 'CTRL',
    badgeColor: 'var(--accent)',
    widgets: [
      { name: 'condition', label: '条件', widget: 'input' },
      {
        name: 'match_type',
        label: '匹配',
        widget: 'select',
        options: [
          { value: 'contains', label: '包含' },
          { value: 'equals', label: '等于' },
          { value: 'regex', label: '正则' },
          { value: 'not_empty', label: '非空' },
        ],
      },
    ],
  },
  python_exec: {
    type: 'python_exec',
    label: 'Python 执行',
    category: 'utility',
    badge: '代码',
    badgeColor: 'var(--accent-2)',
    widgets: [
      { name: 'code', label: 'code', widget: 'textarea', rows: 8, default: 'print("Hello World")' },
    ],
  },
}

export interface NodeCategoryDef {
  name: string
  label: string
  color: string
  nodes: { type: NodeType; dotColor: string }[]
}

export const NODE_CATEGORIES: NodeCategoryDef[] = [
  {
    name: 'ai',
    label: 'AI',
    color: 'var(--purple)',
    nodes: [
      { type: 'llm', dotColor: 'var(--purple)' },
      { type: 'prompt_template', dotColor: 'var(--purple)' },
      { type: 'agent', dotColor: 'var(--purple)' },
    ],
  },
  {
    name: 'control',
    label: '控制流',
    color: 'var(--accent)',
    nodes: [
      { type: 'if_else', dotColor: 'var(--accent)' },
    ],
  },
  {
    name: 'utility',
    label: '工具',
    color: 'var(--accent-2)',
    nodes: [
      { type: 'python_exec', dotColor: 'var(--accent-2)' },
    ],
  },
  {
    name: 'image',
    label: '图像',
    color: 'var(--info)',
    nodes: [
      { type: 'image_output', dotColor: 'var(--info)' },
    ],
  },
]

/** Plugin categories added dynamically from API */
export const PLUGIN_CATEGORIES: NodeCategoryDef[] = []

// Callbacks to notify nodeTypes.ts when plugin defs are loaded
const _onPluginLoadCallbacks: Array<() => void> = []
export function onPluginDefsLoaded(cb: () => void) {
  _onPluginLoadCallbacks.push(cb)
}

/** Category color mapping for plugin nodes */
const CATEGORY_COLORS: Record<string, string> = {
  tts: 'var(--warn)',
  ai: 'var(--purple)',
  audio: 'var(--info)',
  control: 'var(--accent)',
  utility: 'var(--accent-2)',
  image: 'var(--info)',
}

interface PluginNodeDef {
  label: string
  category: string
  badge: string
  badgeColor: string
  inputs?: PortDef[]
  outputs?: PortDef[]
  widgets?: WidgetDef[]
  _package?: string
}

/**
 * Called on app startup to merge plugin node definitions from the backend API.
 * Registers definitions in DECLARATIVE_NODES, port definitions in NODE_DEFS,
 * and categories in PLUGIN_CATEGORIES.
 */
export async function loadPluginDefinitions(): Promise<void> {
  try {
    const resp = await fetch('/api/v1/nodes/definitions')
    if (!resp.ok) return
    const defs: Record<string, PluginNodeDef> = await resp.json()

    // Track which categories we need to add
    const categoryMap: Record<string, { type: NodeType; dotColor: string }[]> = {}

    for (const [nodeType, def] of Object.entries(defs)) {
      // Skip if already hardcoded
      if (DECLARATIVE_NODES[nodeType]) continue

      // Register as declarative node.
      DECLARATIVE_NODES[nodeType] = {
        type: nodeType,
        label: def.label,
        category: def.category,
        badge: def.badge,
        badgeColor: def.badgeColor,
        widgets: (def.widgets ?? []) as WidgetDef[],
      }

      // Register port definitions in NODE_DEFS
      NODE_DEFS[nodeType] = {
        type: nodeType,
        label: def.label,
        inputs: (def.inputs ?? []) as PortDef[],
        outputs: (def.outputs ?? []) as PortDef[],
      }

      // Collect category entries
      const cat = def.category || 'other'
      if (!categoryMap[cat]) categoryMap[cat] = []
      categoryMap[cat].push({
        type: nodeType,
        dotColor: def.badgeColor || CATEGORY_COLORS[cat] || 'var(--muted)',
      })
    }

    // Build plugin categories
    PLUGIN_CATEGORIES.length = 0
    for (const [catName, nodes] of Object.entries(categoryMap)) {
      PLUGIN_CATEGORIES.push({
        name: `plugin:${catName}`,
        label: catName.toUpperCase(),
        color: CATEGORY_COLORS[catName] || 'var(--warn)',
        nodes,
      })
    }

    // Notify subscribers
    for (const cb of _onPluginLoadCallbacks) cb()
  } catch (e) {
    console.warn('Failed to load plugin node definitions:', e)
  }
}
