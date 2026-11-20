import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, X, Copy, RefreshCw, Download, DownloadCloud, Columns2, ExternalLink, Film, Image as ImageIcon, Plus, Trash2 } from 'lucide-react'
import { useConfirmStore } from '../../stores/confirm'
import { useLightboxStore, type LightboxMeta } from '../../stores/lightbox'
import { IDENTITY, clampPan, zoomAt, type ZoomState } from './lightboxZoom'
import { useToastStore } from '../../stores/toast'
import { apiFetch } from '../../api/client'
import { copyTextOrToast } from '../../utils/clipboard'

// 全屏图片预览(对齐 Infinite-Canvas):←/→ 切上/下一张,Esc/点击空白关闭。
// 滚轮缩放(1–6x,光标为锚)+ 放大后拖拽平移 + 双击复位 + 右侧元信息面板(prompt/分辨率/时长/重跑)。
// 单例,挂在 NodeEditor;数据来自 useLightboxStore。

/** 可缩放/平移的图。靠父级 `key={index}` 切图时 remount 复位缩放(无需 effect 重置)。 */
function ZoomableImage({ src, onNatural }: { src: string; onNatural?: (w: number, h: number) => void }) {
  const [zoom, setZoom] = useState<ZoomState>(IDENTITY)
  const [dragging, setDragging] = useState(false)
  const imgRef = useRef<HTMLImageElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null)
  const zoomed = zoom.scale > 1

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const cx = e.clientX - window.innerWidth / 2
      const cy = e.clientY - window.innerHeight / 2
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15
      setZoom((z) => {
        const zoomedSt = zoomAt(z, factor, cx, cy)
        const img = imgRef.current
        return img ? clampPan(zoomedSt, img.clientWidth, img.clientHeight) : zoomedSt
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!drag.current) return
      const img = imgRef.current
      setZoom((z) => {
        const moved = { scale: z.scale, tx: drag.current!.tx + (e.clientX - drag.current!.x), ty: drag.current!.ty + (e.clientY - drag.current!.y) }
        return img ? clampPan(moved, img.clientWidth, img.clientHeight) : moved
      })
    }
    const onUp = () => { drag.current = null; setDragging(false) }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  }, [])

  return (
    <div ref={wrapRef} onClick={(e) => e.stopPropagation()} style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <img
        ref={imgRef}
        src={src}
        alt="preview"
        draggable={false}
        onLoad={(e) => onNatural?.(e.currentTarget.naturalWidth, e.currentTarget.naturalHeight)}
        onDoubleClick={(e) => { e.stopPropagation(); setZoom(IDENTITY) }}
        onMouseDown={(e) => {
          if (!zoomed) return
          e.preventDefault(); e.stopPropagation()
          drag.current = { x: e.clientX, y: e.clientY, tx: zoom.tx, ty: zoom.ty }
          setDragging(true)
        }}
        style={{
          maxWidth: 'var(--lightbox-media-width, 92vw)', maxHeight: 'var(--lightbox-media-height, 92vh)', objectFit: 'contain',
          transform: `translate(${zoom.tx}px, ${zoom.ty}px) scale(${zoom.scale})`,
          transformOrigin: 'center center',
          transition: dragging ? 'none' : 'transform 0.08s',
          cursor: zoomed ? (dragging ? 'grabbing' : 'grab') : 'default',
          willChange: 'transform',
        }}
      />
    </div>
  )
}

/** 视频产物(Task 11:comfy bridge 视频节点)。不缩放/平移,原生 controls 播放。 */
function VideoView({ src, poster }: { src: string; poster?: string }) {
  const [failed, setFailed] = useState(false)
  return (
    <div onClick={(event) => event.stopPropagation()} style={{ display: 'grid', justifyItems: 'center' }}>
      {failed ? (
        <div role="status" style={{ color: '#e2e6ee', padding: 16, textAlign: 'center' }}>
          <p>视频暂时无法播放，文件可能已被清理或格式不受支持。</p>
          <a href={src} target="_blank" rel="noreferrer" style={{ textDecoration: 'underline' }}>打开原视频</a>
        </div>
      ) : <video
        controls autoPlay playsInline preload="metadata" src={src} poster={poster}
        onError={() => setFailed(true)}
        style={{ maxWidth: 'var(--lightbox-media-width, 92vw)', maxHeight: 'var(--lightbox-media-height, 92vh)' }}
      />}
    </div>
  )
}

/** 前后对比:back 底图定尺寸,front 叠加按滑块 clip;拖拽分隔线。 */
function CompareView({ front, back }: { front: string; back: string }) {
  const [pos, setPos] = useState(50)
  const ref = useRef<HTMLDivElement>(null)
  const drag = useRef(false)
  const setFromX = (clientX: number) => {
    const r = ref.current?.getBoundingClientRect()
    if (!r || !r.width) return
    setPos(Math.max(0, Math.min(100, ((clientX - r.left) / r.width) * 100)))
  }
  useEffect(() => {
    const mv = (e: MouseEvent) => { if (drag.current) setFromX(e.clientX) }
    const up = () => { drag.current = false }
    window.addEventListener('mousemove', mv)
    window.addEventListener('mouseup', up)
    return () => { window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up) }
  }, [])
  const tag = (text: string, side: 'left' | 'right') => (
    <span style={{ position: 'absolute', top: 8, [side]: 8, fontSize: 11, color: '#fff', background: 'rgba(0,0,0,0.55)', borderRadius: 4, padding: '2px 7px', pointerEvents: 'none' } as React.CSSProperties}>{text}</span>
  )
  return (
    <div
      ref={ref}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => { e.preventDefault(); drag.current = true; setFromX(e.clientX) }}
      style={{ position: 'relative', maxWidth: 'var(--lightbox-media-width, 92vw)', maxHeight: 'var(--lightbox-media-height, 92vh)', cursor: 'ew-resize', userSelect: 'none' }}
    >
      <img src={back} alt="preview" draggable={false} style={{ display: 'block', maxWidth: 'var(--lightbox-media-width, 92vw)', maxHeight: 'var(--lightbox-media-height, 92vh)', objectFit: 'contain' }} />
      <img src={front} alt="" draggable={false} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', clipPath: `inset(0 ${100 - pos}% 0 0)` }} />
      <div style={{ position: 'absolute', top: 0, bottom: 0, left: `${pos}%`, width: 2, background: '#fff', boxShadow: '0 0 4px rgba(0,0,0,0.6)', transform: 'translateX(-1px)', pointerEvents: 'none' }} />
      <div style={{ position: 'absolute', top: '50%', left: `${pos}%`, width: 24, height: 24, marginLeft: -12, marginTop: -12, borderRadius: '50%', background: '#fff', boxShadow: '0 0 4px rgba(0,0,0,0.6)', pointerEvents: 'none', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, color: '#333' }}>⇄</div>
      {tag('对比', 'left')}
      {tag('当前', 'right')}
    </div>
  )
}

function hasMeta(m: LightboxMeta | undefined): m is LightboxMeta {
  return !!m && (!!m.historyTaskId || !!m.prompt || !!m.resolution || !!m.fields?.length || m.durationMs != null || !!m.onRerun)
}

type GalleryMetadata = Pick<LightboxMeta, 'prompt' | 'fields'> & { tags: string[] }

const parameterLabels: Record<string, string> = {
  seed: '种子',
  input_mode: '功能模式',
  enable_optimization: '提示词优化',
  enable_upscale: '高清放大',
  aspect_ratio: '画幅',
  megapixels: '生成像素（MP）',
}

function HistoryTags({ taskId, tags, onSaved, onSearch }: {
  taskId: string; tags: string[]; onSaved: (tags: string[]) => void; onSearch?: (tag: string) => void
}) {
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  const apply = async (next: string[]) => {
    setSaving(true)
    try {
      const result = await apiFetch<{ tags: string[] }>(`/api/v1/tasks/gallery/${encodeURIComponent(taskId)}/tags`, {
        method: 'PUT', body: JSON.stringify({ tags: next }),
      })
      onSaved(result.tags)
      setDraft('')
    } catch {
      useToastStore.getState().add('标签保存失败，请重试', 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="history-detail-section">
      <h2>标签</h2>
      {!!tags.length && <div className="history-tags">
        {tags.map(tag => <span key={tag} className="history-tag">
          <button type="button" title="在图库中搜索此标签" onClick={() => onSearch?.(tag)}>#{tag}</button>
          <button type="button" aria-label={`移除标签 ${tag}`} disabled={saving}
            onClick={() => void apply(tags.filter(item => item !== tag))}><X size={13} /></button>
        </span>)}
      </div>}
      <form className="history-tag-entry" onSubmit={event => {
        event.preventDefault()
        const tag = draft.trim()
        if (tag && tags.length < 20 && !saving) void apply([...tags, tag])
      }}>
        <input type="text" aria-label="新标签" placeholder="输入标签" maxLength={32} value={draft}
          disabled={saving || tags.length >= 20} onChange={event => setDraft(event.currentTarget.value)} />
        <button type="submit" disabled={saving || !draft.trim() || tags.length >= 20}><Plus size={15} /> 添加</button>
      </form>
    </section>
  )
}

function HistoryThumbnails({ indices, metas, kinds, index, choose, group = false }: {
  indices: number[]
  metas: (LightboxMeta | undefined)[]
  kinds: ('image' | 'video' | undefined)[]
  index: number
  choose: (index: number) => void
  group?: boolean
}) {
  const active = useRef<HTMLButtonElement>(null)
  useEffect(() => active.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' }), [index])
  return (
    <nav className={group ? 'history-group-thumbs' : 'history-side-thumbs'} aria-label={group ? '同任务图集' : '图库缩略图'}
      onClick={event => event.stopPropagation()}>
      {group && <span className="history-group-label">同任务 · {indices.length} 个产物</span>}
      <div className="history-thumbs">
        {indices.map((slot, position) => {
          const url = metas[slot]?.previewUrl
          const Icon = kinds[slot] === 'video' ? Film : ImageIcon
          return <button type="button" key={slot} ref={slot === index ? active : undefined}
            aria-current={slot === index ? 'true' : undefined}
            aria-label={group ? `查看同任务产物 ${position + 1}` : `预览记录 ${metas[slot]?.title ?? metas[slot]?.historyTaskId ?? slot + 1}`}
            onClick={() => choose(slot)}>
            {url ? <img src={url} alt="" decoding="async" loading="lazy"
              onError={event => { event.currentTarget.hidden = true }} /> : null}
            <Icon size={16} />
          </button>
        })}
      </div>
    </nav>
  )
}

function MetaPanel({ meta, fallbackRes, media, tags, onTagsSaved }: {
  meta: LightboxMeta
  fallbackRes: string
  media?: { url: string; video: boolean; download: () => void }
  tags?: string[]
  onTagsSaved?: (tags: string[]) => void
}) {
  const [copied, setCopied] = useState(false)
  const res = meta.resolution || fallbackRes
  const history = !!meta.historyTaskId
  const dateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
  const core = history ? meta.prompt?.match(/(?:核心元素|主要元素)[：:]\s*([^。\n]+(?:。|$)?)/)?.[1]?.trim() : undefined
  const fields = meta.fields?.length ? (
    <dl className={history ? 'history-parameter-list' : undefined}
      style={history ? undefined : { display: 'grid', gap: 5, fontSize: 12 }}>
      {meta.fields.map(({ label, value }, index) => (
        <div key={index} style={history ? undefined : { display: 'flex', gap: 8 }}>
          <dt title={label}>{history ? parameterLabels[label] ?? label : label}</dt>
          <dd>{history && value === 'true' ? '开启' : history && value === 'false' ? '关闭' : value}</dd>
        </div>
      ))}
    </dl>
  ) : null
  return (
    <div
      onClick={(e) => e.stopPropagation()}
      className={history ? 'nowheel history-lightbox-meta' : 'nowheel'}
      style={history ? undefined : {
        position: 'absolute', top: 70, right: 18, width: 280, maxHeight: 'calc(100vh - 100px)',
        overflowY: 'auto', background: 'rgba(20,20,22,0.92)', border: '1px solid rgba(255,255,255,0.15)',
        borderRadius: 10, padding: 14, color: '#fff', fontSize: 12, display: 'flex', flexDirection: 'column', gap: 10,
        backdropFilter: 'blur(4px)',
      }}
    >
      {history && (
        <header>
          <strong>{meta.title}</strong>
          <span className="history-detail-kind">{media?.video ? '视频' : '图片'}</span>
          {meta.createdAt && <time dateTime={meta.createdAt}>生成于 {dateFormat.format(new Date(meta.createdAt))}</time>}
          {meta.expiresAt && <time dateTime={meta.expiresAt}>
            {meta.expired ? '已到期' : '预计到期'} · {dateFormat.format(new Date(meta.expiresAt))}
          </time>}
        </header>
      )}
      {meta.prompt && (
        <section className={history ? 'history-detail-section history-detail-prompt' : undefined}>
          <div className={history ? 'history-prompt-heading' : undefined}
            style={history ? undefined : { display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <h2 style={history ? undefined : { fontSize: 10, opacity: 0.6, flex: 1 }}>提示词</h2>
            <button
              type="button"
              className={history ? 'history-copy-prompt' : undefined}
              onClick={() => {
                void copyTextOrToast(meta.prompt || '').then((ok) => {
                  if (!ok) return
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1200)
                })
              }}
              style={history ? undefined : { display: 'inline-flex', alignItems: 'center', gap: 3, fontSize: 10, color: '#9ecbff' }}
            >
              <Copy size={history ? 14 : 11} />{copied ? '已复制' : history ? '复制提示词' : '复制'}
            </button>
          </div>
          <div className={history ? 'history-prompt-text' : undefined}
            style={history ? undefined : { fontSize: 11.5, lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 160, overflowY: 'auto', opacity: 0.95 }}>
            {meta.prompt}
          </div>
          {core && <div className="history-prompt-core"><strong>核心元素</strong><p>{core}</p></div>}
        </section>
      )}
      {meta.historyTaskId && tags && onTagsSaved && (
        <HistoryTags key={meta.historyTaskId} taskId={meta.historyTaskId} tags={tags} onSaved={onTagsSaved} onSearch={meta.onTagSearch} />
      )}
      {(res || meta.durationMs != null) && (
        <div className={history ? 'history-media-facts' : undefined}
          style={history ? undefined : { display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 11, opacity: 0.85 }}>
          {res && <span>分辨率 {res}</span>}
          {meta.durationMs != null && <span>耗时 {(meta.durationMs / 1000).toFixed(1)}s</span>}
        </div>
      )}
      {fields && (history ? (
        <details className="history-detail-fields history-detail-section" open><summary>生成参数</summary>{fields}</details>
      ) : fields)}
      {history && media && <footer className="history-detail-actions">
        <div>
          {meta.onRerun && <button type="button" className="history-detail-primary" onClick={meta.onRerun}>
            <RefreshCw size={15} /> 沿用参数生成
          </button>}
          {meta.onOpenSource && <button type="button" onClick={meta.onOpenSource}>
            <ExternalLink size={14} /> {meta.sourceLabel}
          </button>}
        </div>
        <div>
          <button type="button" onClick={media.download}><Download size={15} /> 下载{media.video ? '视频' : '原图'}</button>
          <a href={media.url} target="_blank" rel="noreferrer">打开原文件</a>
          {meta.onDelete && <button type="button" className="history-danger" onClick={() => meta.onDelete?.(true)}>
            <Trash2 size={14} /> 永久删除
          </button>}
        </div>
      </footer>}
      {!history && meta.onRerun && (
        <button
          type="button"
          onClick={() => meta.onRerun?.()}
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
            padding: '7px 10px', fontSize: 12, fontWeight: 600, borderRadius: 6, cursor: 'pointer',
            background: 'var(--accent, #4f7cff)', color: 'var(--on-accent, #fff)', border: 'none',
          }}
        >
          <RefreshCw size={13} />重跑(相同参数)
        </button>
      )}
    </div>
  )
}

export default function Lightbox() {
  const open = useLightboxStore((s) => s.open)
  const images = useLightboxStore((s) => s.images)
  const metas = useLightboxStore((s) => s.metas)
  const kinds = useLightboxStore((s) => s.kinds)
  const index = useLightboxStore((s) => s.index)
  const close = useLightboxStore((s) => s.close)
  const select = useLightboxStore((s) => s.select)
  const next = useLightboxStore((s) => s.next)
  const prev = useLightboxStore((s) => s.prev)
  const [natural, setNatural] = useState<{ url: string; w: number; h: number } | null>(null)
  const [compare, setCompare] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (useConfirmStore.getState().current || (e.target instanceof HTMLElement &&
        (e.target.isContentEditable || e.target.matches('input,textarea,select')))) return
      if (e.key === 'Escape') { e.preventDefault(); close() }
      else if (e.key === 'ArrowRight') { e.preventDefault(); next() }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); prev() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, close, next, prev])

  const currentMeta = metas[index]
  const historyTaskId = open ? currentMeta?.historyTaskId : undefined
  const [historyMeta, setHistoryMeta] = useState<{ id: string; info: GalleryMetadata }>()
  useEffect(() => {
    if (!historyTaskId) return
    const controller = new AbortController()
    apiFetch<GalleryMetadata>(
      `/api/v1/tasks/gallery/${encodeURIComponent(historyTaskId)}/metadata`, { signal: controller.signal },
    ).then((info) => {
      if (!controller.signal.aborted) setHistoryMeta({ id: historyTaskId, info })
    }).catch(() => undefined)
    return () => controller.abort()
  }, [historyTaskId])

  if (!open || !images.length) return null
  const url = images[index]
  const meta = historyMeta && historyMeta.id === historyTaskId
    ? { ...currentMeta, ...historyMeta.info }
    : currentMeta
  const kind = kinds[index] ?? 'image'
  const isVideo = kind === 'video'
  const multi = images.length > 1
  const group = historyTaskId ? metas.flatMap((item, slot) => item?.historyTaskId === historyTaskId ? [slot] : []) : []
  const first = group[0] ?? index
  const entries = metas.flatMap((item, slot) => !slot || item?.historyTaskId !== metas[slot - 1]?.historyTaskId ? [slot] : [])
  const start = Math.max(0, entries.indexOf(first) - 6)
  const nearby = entries.slice(start, start + 13)
  const choose = (slot: number) => {
    select(slot)
    setCompare(false)
  }
  // 显式 compareBase(输入/源图,如编辑/超分流)优先于多图「与上一张」。有它时单图
  // 也能对比(对齐 IC 的「生成图 vs 输入图」);两者都无则对比按钮不出现。
  // 视频产物不支持缩放/对比(Task 11)。
  const explicitBase = meta?.compareBase && meta.compareBase !== url ? meta.compareBase : undefined
  const compareIndex = index === 0 ? 1 : index - 1
  const nearbyImage = multi && kinds[compareIndex] !== 'video' ? images[compareIndex] : undefined
  const compareBase = explicitBase ?? nearbyImage
  const canCompare = !isVideo && !!compareBase
  const showCompare = compare && canCompare

  const basename = (u: string): string => {
    try {
      const path = new URL(u, window.location.href).pathname
      return decodeURIComponent(path.split('/').pop() || '') || `image-${index + 1}.png`
    } catch { return `image-${index + 1}.png` }
  }
  const downloadOne = (u: string) => {
    const a = document.createElement('a')
    a.href = u; a.download = basename(u)
    document.body.appendChild(a); a.click(); a.remove()
  }
  const downloadAll = (urls = images) => urls.forEach((url, offset) => setTimeout(() => downloadOne(url), offset * 200))

  const navBtn = (onClick: () => void, side: 'left' | 'right', icon: React.ReactNode) => (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick() }}
      aria-label={side === 'left' ? '上一张' : '下一张'}
      style={{
        position: 'absolute', top: '50%', transform: 'translateY(-50%)', [side]: !historyTaskId ? 18 : side === 'right' ? 'calc(18px + var(--history-meta-width))' : 'calc(18px + var(--history-strip-width))',
        width: 44, height: 44, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.25)', color: '#fff', cursor: 'pointer',
      } as React.CSSProperties}
    >
      {icon}
    </button>
  )

  const media = isVideo
    ? <VideoView key={index} src={url} poster={meta?.poster} />
    : showCompare && compareBase
      ? <CompareView key={`cmp-${index}`} front={url} back={compareBase} />
      : <ZoomableImage key={index} src={url} onNatural={(w, h) => setNatural({ url, w, h })} />

  return (
    <div
      onClick={close}
      className={historyTaskId ? 'history-lightbox' : undefined}
      style={{
        position: 'fixed', inset: 0, zIndex: 9999, background: historyTaskId ? 'var(--gallery-backdrop)' : 'rgba(0,0,0,0.85)',
        display: historyTaskId ? 'grid' : 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'zoom-out',
      }}
    >
      {historyTaskId ? <>
        {nearby.length > 1 && <HistoryThumbnails indices={nearby} metas={metas} kinds={kinds} index={first} choose={choose} />}
        <div className="history-lightbox-stage">
          <div className="history-lightbox-media">{media}</div>
          {group.length > 1 && <HistoryThumbnails indices={group} metas={metas} kinds={kinds} index={index} choose={choose} group />}
        </div>
      </> : media}

      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); close() }}
        aria-label="关闭"
        style={{
          position: 'absolute', top: 18, right: 18, width: 40, height: 40, borderRadius: '50%',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.25)', color: '#fff', cursor: 'pointer',
        }}
      >
        <X size={20} />
      </button>

      {hasMeta(meta) && (
        <MetaPanel meta={meta} fallbackRes={!isVideo && natural?.url === url ? `${natural.w}×${natural.h}` : ''}
          tags={historyMeta?.id === historyTaskId ? historyMeta?.info.tags : undefined}
          onTagsSaved={tags => setHistoryMeta(current => current && current.id === historyTaskId
            ? { ...current, info: { ...current.info, tags } } : current)}
          media={historyTaskId ? { url, video: isVideo, download: () => downloadOne(url) } : undefined} />
      )}

      {multi && !showCompare && (
        <>
          {navBtn(prev, 'left', <ChevronLeft size={24} />)}
          {navBtn(next, 'right', <ChevronRight size={24} />)}
        </>
      )}

      {/* 底部工具栏:对比 / 下载 / 下载全部 + 计数 */}
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          position: 'absolute', bottom: historyTaskId ? undefined : 20, top: historyTaskId ? 14 : undefined,
          left: historyTaskId ? 'calc((100% + var(--history-strip-width) - var(--history-meta-width)) / 2)' : '50%',
          transform: 'translateX(-50%)',
          display: 'flex', alignItems: 'center', gap: 8,
          background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.2)',
          padding: '5px 8px', borderRadius: 999,
        }}
      >
        {canCompare && (
          <ToolBtn
            active={showCompare}
            onClick={() => setCompare((c) => !c)}
            title={explicitBase ? '前后对比(与输入图)' : '前后对比(与上一张)'}
          >
            <Columns2 size={15} />
          </ToolBtn>
        )}
        {!historyTaskId && <ToolBtn onClick={() => downloadOne(url)} title="下载当前"><Download size={15} /></ToolBtn>}
        {multi && !historyTaskId && <ToolBtn onClick={() => downloadAll()} title={`下载全部(${images.length})`}><DownloadCloud size={15} /></ToolBtn>}
        {group.length > 1 && <ToolBtn onClick={() => downloadAll(group.map(slot => images[slot]))}
          title={`下载本组(${group.length})`}><DownloadCloud size={15} /></ToolBtn>}
        {multi && (
          <span style={{ fontSize: 12, color: '#fff', fontFamily: 'var(--mono)', padding: '0 4px' }}>
            {index + 1} / {images.length}
          </span>
        )}
      </div>
    </div>
  )
}

function ToolBtn({ children, onClick, title, active }: { children: React.ReactNode; onClick: () => void; title: string; active?: boolean }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={(e) => { e.stopPropagation(); onClick() }}
      style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        width: 32, height: 32, borderRadius: '50%', cursor: 'pointer',
        background: active ? 'var(--accent, #4f7cff)' : 'transparent',
        border: 'none', color: '#fff',
      }}
    >
      {children}
    </button>
  )
}
