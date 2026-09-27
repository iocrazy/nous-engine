// 收敛后(spec 2026-05-21):Family B 的小写 unet/clip/vae 端口已删。曾用大写
// MODEL/CLIP/VAE/CONDITIONING/LATENT(经 plugin defs 走字符串,绕过本 union)服务于
// flux2-components 细粒度图,该图已随自建图像引擎删除,当前没有节点再用这套大写类型。
export type PortType = 'text' | 'audio' | 'image' | 'message' | 'data' | 'any'

/** Node type identifier. Built-in types are listed below; plugin packages can add more at runtime. */
export type NodeType = string

/** Built-in node types (for reference / type narrowing). */
export type BuiltinNodeType =
  | 'text_input'
  | 'text_output'
  | 'multimodal_input'
  | 'ref_audio'
  | 'tts_engine'
  | 'resample'
  | 'mixer'
  | 'concat'
  | 'bgm_mix'
  | 'output'
  | 'llm'
  | 'prompt_template'
  | 'agent'
  | 'if_else'
  | 'python_exec'
  | 'image_output'
  | 'image_compare'

export interface PortDef {
  id: string
  type: PortType
  label: string
}

export interface NodeDef {
  type: NodeType
  label: string
  inputs: PortDef[]
  outputs: PortDef[]
}

export const NODE_DEFS: Record<NodeType, NodeDef> = {
  text_input: {
    type: 'text_input',
    label: '文本输入',
    inputs: [],
    outputs: [{ id: 'text', type: 'text', label: '文本' }],
  },
  text_output: {
    type: 'text_output',
    label: '文本输出',
    inputs: [{ id: 'text', type: 'text', label: '文本' }],
    outputs: [],
  },
  multimodal_input: {
    type: 'multimodal_input',
    label: '多模态输入',
    inputs: [],
    outputs: [
      { id: 'text', type: 'text', label: '文本' },
      { id: 'image', type: 'data', label: '图片' },
      { id: 'audio', type: 'audio', label: '音频' },
    ],
  },
  ref_audio: {
    type: 'ref_audio',
    label: '参考音频',
    inputs: [],
    outputs: [{ id: 'audio', type: 'audio', label: '音频' }],
  },
  tts_engine: {
    type: 'tts_engine',
    label: 'TTS 引擎',
    inputs: [
      { id: 'text', type: 'text', label: '文本' },
      { id: 'ref_audio', type: 'audio', label: '参考音频' },
    ],
    outputs: [{ id: 'audio', type: 'audio', label: '音频' }],
  },
  resample: {
    type: 'resample',
    label: '重采样',
    inputs: [{ id: 'audio', type: 'audio', label: '输入' }],
    outputs: [{ id: 'audio', type: 'audio', label: '输出' }],
  },
  mixer: {
    type: 'mixer',
    label: '混音',
    inputs: [
      { id: 'audio_1', type: 'audio', label: '轨道 1' },
      { id: 'audio_2', type: 'audio', label: '轨道 2' },
    ],
    outputs: [{ id: 'audio', type: 'audio', label: '输出' }],
  },
  concat: {
    type: 'concat',
    label: '拼接',
    inputs: [
      { id: 'audio_1', type: 'audio', label: '音频 1' },
      { id: 'audio_2', type: 'audio', label: '音频 2' },
    ],
    outputs: [{ id: 'audio', type: 'audio', label: '输出' }],
  },
  bgm_mix: {
    type: 'bgm_mix',
    label: 'BGM 混合',
    inputs: [
      { id: 'speech', type: 'audio', label: '语音' },
      { id: 'bgm', type: 'audio', label: 'BGM' },
    ],
    outputs: [{ id: 'audio', type: 'audio', label: '输出' }],
  },
  output: {
    type: 'output',
    label: '输出播放',
    inputs: [{ id: 'audio', type: 'audio', label: '音频' }],
    outputs: [],
  },
  llm: {
    type: 'llm',
    label: 'LLM',
    inputs: [
      { id: 'prompt', type: 'text', label: '提示' },
      { id: 'image', type: 'data', label: '图片' },
      { id: 'audio', type: 'audio', label: '音频' },
    ],
    outputs: [{ id: 'text', type: 'text', label: '输出' }],
  },
  prompt_template: {
    type: 'prompt_template',
    label: '提示模板',
    inputs: [{ id: 'text', type: 'text', label: '输入' }],
    outputs: [{ id: 'text', type: 'text', label: '输出' }],
  },
  agent: {
    type: 'agent',
    label: 'Agent',
    inputs: [{ id: 'text', type: 'text', label: '输入' }],
    outputs: [{ id: 'text', type: 'text', label: '文本' }],
  },
  if_else: {
    type: 'if_else',
    label: '条件分支',
    inputs: [{ id: 'input', type: 'text', label: '输入' }],
    outputs: [
      { id: 'true', type: 'text', label: '真' },
      { id: 'false', type: 'text', label: '假' },
    ],
  },
  python_exec: {
    type: 'python_exec',
    label: 'Python 执行',
    inputs: [{ id: 'text', type: 'text', label: '输入' }],
    outputs: [{ id: 'text', type: 'text', label: '输出' }],
  },
  image_output: {
    type: 'image_output',
    label: '图像输出',
    inputs: [{ id: 'image', type: 'image', label: '图像' }],
    outputs: [],
  },
  image_compare: {
    type: 'image_compare',
    label: '图像对比',
    inputs: [
      { id: 'image_a', type: 'image', label: '图像 A' },
      { id: 'image_b', type: 'image', label: '图像 B' },
    ],
    outputs: [],
  },
}

export interface WorkflowNode {
  id: string
  type: NodeType
  data: Record<string, unknown>
  position: { x: number; y: number }
}

export interface WorkflowEdge {
  id: string
  source: string
  sourceHandle: string
  target: string
  targetHandle: string
}

/** 节点分组(ComfyUI 式可视框):纯展示,不入执行图。拖动组头移动框内节点。
 * 坐标/尺寸都是画布(flow)坐标系像素。 */
export interface WorkflowGroup {
  id: string
  title: string
  x: number
  y: number
  width: number
  height: number
  /** 边框/标题色(CSS color)。 */
  color: string
  /** 显式成员节点 id(Ctrl+G 写入)。有则分组框按成员实测包围盒自适应 + 拖动成员一起移动;
   *  无(legacy)则回退到「按 position 落框内」隐式判定 + 固定 x/y/w/h。 */
  nodeIds?: string[]
}

export interface Workflow {
  id: string
  name: string
  description?: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  groups?: WorkflowGroup[]
  is_template?: boolean
  status?: 'draft' | 'published'
}
