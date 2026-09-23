import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, X, Copy, RefreshCw, Download, DownloadCloud, Columns2 } from 'lucide-react'
import { useLightboxStore, type LightboxMeta } from '../../stores/lightbox'
import { IDENTITY, clampPan, zoomAt, type ZoomState } from './lightboxZoom'
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
          maxWidth: '92vw', maxHeight: '92vh', objectFit: 'contain',
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
function VideoView({ src }: { src: string }) {
  return (
    <div onClick={(e) => e.stopPropagation()} style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <video
        controls
        autoPlay
        src={src}
        style={{ maxWidth: '92vw', maxHeight: '92vh' }}
      />
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
      style={{ position: 'relative', maxWidth: '92vw', maxHeight: '92vh', cursor: 'ew-resize', userSelect: 'none' }}
    >
      <img src={back} alt="preview" draggable={false} style={{ display: 'block', maxWidth: '92vw', maxHeight: '92vh', objectFit: 'contain' }} />
      <img src={front} alt="" draggable={false} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', clipPath: `inset(0 ${100 - pos}% 0 0)` }} />
      <div style={{ position: 'absolute', top: 0, bottom: 0, left: `${pos}%`, width: 2, background: '#fff', boxShadow: '0 0 4px rgba(0,0,0,0.6)', transform: 'translateX(-1px)', pointerEvents: 'none' }} />
      <div style={{ position: 'absolute', top: '50%', left: `${pos}%`, width: 24, height: 24, marginLeft: -12, marginTop: -12, borderRadius: '50%', background: '#fff', boxShadow: '0 0 4px rgba(0,0,0,0.6)', pointerEvents: 'none', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, color: '#333' }}>⇄</div>
      {tag('对比', 'left')}
      {tag('当前', 'right')}
    </div>
  )
}

function hasMeta(m: LightboxMeta | undefined): m is LightboxMeta {
  return !!m && (!!m.prompt || !!m.resolution || !!m.fields?.length || m.durationMs != null || !!m.onRerun)
}

function MetaPanel({ meta, fallbackRes }: { meta: LightboxMeta; fallbackRes: string }) {
  const [copied, setCopied] = useState(false)
  const res = meta.resolution || fallbackRes
  return (
    <div
      onClick={(e) => e.stopPropagation()}
      className="nowheel"
      style={{
        position: 'absolute', top: 70, right: 18, width: 280, maxHeight: 'calc(100vh - 100px)',
        overflowY: 'auto', background: 'rgba(20,20,22,0.92)', border: '1px solid rgba(255,255,255,0.15)',
        borderRadius: 10, padding: 14, color: '#fff', fontSize: 12, display: 'flex', flexDirection: 'column', gap: 10,
        backdropFilter: 'blur(4px)',
      }}
    >
      {meta.prompt && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span style={{ fontSize: 10, opacity: 0.6, textTransform: 'uppercase', letterSpacing: 0.5, flex: 1 }}>提示词</span>
            <button
              type="button"
              onClick={() => {
                void copyTextOrToast(meta.prompt || '').then((ok) => {
                  if (!ok) return
                  setCopied(true); setTimeout(() => setCopied(false), 1200)
                })
              }}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 3, fontSize: 10, background: 'transparent', border: 'none', color: '#9ecbff', cursor: 'pointer' }}
            >
              <Copy size={11} />{copied ? '已复制' : '复制'}
            </button>
          </div>
          <div style={{ fontSize: 11.5, lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 160, overflowY: 'auto', opacity: 0.95 }}>
            {meta.prompt}
          </div>
        </div>
      )}
      {(res || meta.durationMs != null) && (
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 11, opacity: 0.85 }}>
          {res && <span>分辨率 {res}</span>}
          {meta.durationMs != null && <span>耗时 {(meta.durationMs / 1000).toFixed(1)}s</span>}
        </div>
      )}
      {meta.fields && meta.fields.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
          {meta.fields.map((f, i) => (
            <div key={i} style={{ display: 'flex', gap: 8 }}>
              <span style={{ opacity: 0.55, minWidth: 64 }}>{f.label}</span>
              <span style={{ flex: 1, wordBreak: 'break-word', opacity: 0.95 }}>{f.value}</span>
            </div>
          ))}
        </div>
      )}
      {meta.onRerun && (
        <button
          type="button"
          onClick={() => meta.onRerun?.()}
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
            padding: '7px 10px', fontSize: 12, fontWeight: 600, borderRadius: 6, cursor: 'pointer',
            background: 'var(--accent, #4f7cff)', color: '#fff', border: 'none',
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
  const next = useLightboxStore((s) => s.next)
  const prev = useLightboxStore((s) => s.prev)
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  const [compare, setCompare] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); close() }
      else if (e.key === 'ArrowRight') { e.preventDefault(); next() }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); prev() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, close, next, prev])

  if (!open || !images.length) return null
  const url = images[index]
  const meta = metas[index]
  const kind = kinds[index] ?? 'image'
  const isVideo = kind === 'video'
  const multi = images.length > 1
  // 显式 compareBase(输入/源图,如编辑/超分流)优先于多图「与上一张」。有它时单图
  // 也能对比(对齐 IC 的「生成图 vs 输入图」);两者都无则对比按钮不出现。
  // 视频产物不支持缩放/对比(Task 11)。
  const explicitBase = meta?.compareBase && meta.compareBase !== url ? meta.compareBase : undefined
  const canCompare = !isVideo && (!!explicitBase || multi)
  const compareBase = explicitBase ?? (multi ? images[index === 0 ? 1 : index - 1] : url)
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
  const downloadAll = () => images.forEach((u, i) => setTimeout(() => downloadOne(u), i * 200))

  const navBtn = (onClick: () => void, side: 'left' | 'right', icon: React.ReactNode) => (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick() }}
      aria-label={side === 'left' ? '上一张' : '下一张'}
      style={{
        position: 'absolute', top: '50%', transform: 'translateY(-50%)', [side]: 18,
        width: 44, height: 44, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.25)', color: '#fff', cursor: 'pointer',
      } as React.CSSProperties}
    >
      {icon}
    </button>
  )

  return (
    <div
      onClick={close}
      style={{
        position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(0,0,0,0.85)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'zoom-out',
      }}
    >
      {/* key=index → 切图 remount,缩放/平移自动复位;对比模式换成 CompareView;视频走独立分支 */}
      {isVideo
        ? <VideoView key={index} src={url} />
        : showCompare
          ? <CompareView key={`cmp-${index}`} front={url} back={compareBase} />
          : <ZoomableImage key={index} src={url} onNatural={(w, h) => setNatural({ w, h })} />}

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
        <MetaPanel meta={meta} fallbackRes={natural ? `${natural.w}×${natural.h}` : ''} />
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
          position: 'absolute', bottom: 20, left: '50%', transform: 'translateX(-50%)',
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
        <ToolBtn onClick={() => downloadOne(url)} title="下载当前"><Download size={15} /></ToolBtn>
        {multi && <ToolBtn onClick={downloadAll} title={`下载全部(${images.length})`}><DownloadCloud size={15} /></ToolBtn>}
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
