import { create } from 'zustand'
import { useWorkspaceStore } from './workspace'

// 全局图片预览 lightbox(对齐 Infinite-Canvas:Z/方向键切图 + 缩放平移 + 元信息面板)。
// 画布上图像节点点图 → 收集当前工作流全部图;/history 画廊点图 → 显式给图集 + 每图元信息。

// 节点 data 里可能承载图片 url 的字段(ImageOutput / ImageCompare / 输入图等)。
const IMAGE_KEYS = ['image_url', 'image_a_url', 'image_b_url', 'image', 'dataUrl', 'url']

/** 每张图的元信息(灯箱右侧面板用)。来源:画布节点 data 或 /history 的 ExecutionTask。 */
export interface LightboxMeta {
  /** 长文本(提示词),面板里带复制。 */
  prompt?: string
  /** 通用键值(seed/steps/cfg 或 input_json 摘要)。 */
  fields?: Array<{ label: string; value: string }>
  /** "W×H";缺省时灯箱用 <img> naturalWidth×naturalHeight 兜底。 */
  resolution?: string
  durationMs?: number | null
  historyTaskId?: string
  title?: string
  model?: string
  keyParameters?: string[]
  createdAt?: string
  expiresAt?: string
  expired?: boolean
  poster?: string
  previewUrl?: string
  onOpenSource?: () => void
  sourceLabel?: string
  onTagSearch?: (tag: string) => void
  onDelete?: (permanent: boolean) => void
  /** 「重跑」回调(有则面板显示重跑按钮)。 */
  onRerun?: () => void
  /** 前后对比的基准图 url(对齐 Infinite-Canvas:生成图 vs 输入/源图)。有值则
   *  对比按钮无条件出现(不依赖多图),CompareView 用它作底图;缺省时回退到
   *  多图模式的「与上一张」。编辑/超分流由 ImageOutputNode 上溯源图填入。 */
  compareBase?: string
}

/** 按节点顺序收集当前工作流全部图片 url(去重)。 */
export function collectWorkflowImages(): string[] {
  const wf = useWorkspaceStore.getState().getActiveWorkflow()
  const urls: string[] = []
  // images 可能是 url 字符串数组,也可能是 image_output 累积网格的 {url,...meta} 对象数组。
  const push = (v: unknown) => {
    const u = typeof v === 'string' ? v
      : (v && typeof v === 'object' ? (v as { url?: unknown }).url : null)
    if (typeof u === 'string' && u && !urls.includes(u)) urls.push(u)
  }
  for (const n of wf.nodes) {
    const d = n.data as Record<string, unknown> | undefined
    if (!d) continue
    for (const k of IMAGE_KEYS) push(d[k])
    if (Array.isArray(d.images)) d.images.forEach(push)
  }
  return urls
}

export interface LightboxItem {
  url: string
  meta?: LightboxMeta
  /** 产物类型;缺省 'image'(向后兼容 —— 画布节点/旧调用都只给 url+meta)。
   *  Task 11:comfy bridge 视频产物用 'video',灯箱渲染 <video> 而非可缩放 <img>。 */
  kind?: 'image' | 'video'
}

interface LightboxState {
  open: boolean
  images: string[]
  metas: (LightboxMeta | undefined)[]
  kinds: ('image' | 'video' | undefined)[]
  index: number
  /** 从某张图 url 打开:自动收集同工作流全部图并定位;meta 给被点的那张。 */
  openFromUrl: (url: string, meta?: LightboxMeta) => void
  /** 显式给定图集(带每图元信息)打开 —— /history 画廊用。 */
  openItems: (items: LightboxItem[], index: number) => void
  close: () => void
  select: (index: number) => void
  next: () => void
  prev: () => void
}

export const useLightboxStore = create<LightboxState>((set, get) => ({
  open: false,
  images: [],
  metas: [],
  kinds: [],
  index: 0,
  openFromUrl: (url, meta) => {
    const all = collectWorkflowImages()
    let images = all
    let index = all.indexOf(url)
    if (index < 0) { images = [url]; index = 0 }
    const metas = images.map((u) => (u === url ? meta : undefined))
    set({ open: true, images, metas, kinds: images.map(() => undefined), index })
  },
  openItems: (items, index) => set({
    open: true,
    images: items.map((it) => it.url),
    metas: items.map((it) => it.meta),
    kinds: items.map((it) => it.kind),
    index: Math.max(0, Math.min(index, items.length - 1)),
  }),
  close: () => set({ open: false }),
  select: (index) => set((state) => index >= 0 && index < state.images.length ? { index } : state),
  next: () => { const { images, index } = get(); if (images.length) set({ index: (index + 1) % images.length }) },
  prev: () => { const { images, index } = get(); if (images.length) set({ index: (index - 1 + images.length) % images.length }) },
}))
