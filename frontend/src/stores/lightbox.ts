import { create } from 'zustand'

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

export interface LightboxItem {
  url: string
  meta?: LightboxMeta
  /** 产物类型;缺省 'image'(向后兼容 —— 画布节点/旧调用都只给 url+meta)。
   *  Task 11:comfy bridge 视频产物用 'video',灯箱渲染 <video> 而非可缩放 <img>。 */
  kind?: 'image' | 'video'
}

type LoadGalleryPage = () => Promise<{ items: LightboxItem[]; hasNextPage: boolean }>

interface LightboxState {
  open: boolean
  images: string[]
  metas: (LightboxMeta | undefined)[]
  kinds: ('image' | 'video' | undefined)[]
  index: number
  /** 显式给定图集(带每图元信息)打开 —— /history 画廊用。 */
  openItems: (items: LightboxItem[], index: number, loadPage?: LoadGalleryPage) => void
  loadMore?: () => Promise<void>
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
  openItems: (items, index, loadPage) => {
    const loadMore = loadPage && (async () => {
      const page = await loadPage()
      set(state => state.loadMore === loadMore ? {
        images: [...state.images, ...page.items.map(item => item.url)],
        metas: [...state.metas, ...page.items.map(item => item.meta)],
        kinds: [...state.kinds, ...page.items.map(item => item.kind)],
        loadMore: page.hasNextPage ? loadMore : undefined,
      } : state)
    })
    set({
      open: true,
      images: items.map(item => item.url),
      metas: items.map(item => item.meta),
      kinds: items.map(item => item.kind),
      index: Math.max(0, Math.min(index, items.length - 1)),
      loadMore,
    })
  },
  close: () => set({ open: false, loadMore: undefined }),
  select: (index) => set((state) => index >= 0 && index < state.images.length ? { index } : state),
  next: () => { const { images, index } = get(); if (images.length) set({ index: (index + 1) % images.length }) },
  prev: () => { const { images, index } = get(); if (images.length) set({ index: (index - 1 + images.length) % images.length }) },
}))
