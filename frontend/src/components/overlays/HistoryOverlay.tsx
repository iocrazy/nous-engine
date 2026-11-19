import { useRef, useState } from 'react'
import { X, RefreshCw, Trash2, Play } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { usePanelStore } from '../../stores/panel'
import { confirmDialog } from '../../stores/confirm'
import { getTask, useImageTasks, useDeleteTask, type GalleryTask } from '../../api/tasks'
import { useToastStore } from '../../stores/toast'
import { useServices } from '../../api/services'
import { useLightboxStore, type LightboxItem, type LightboxMeta } from '../../stores/lightbox'

export default function HistoryOverlay() {
  const setOverlay = usePanelStore((s) => s.setOverlay)
  const navigate = useNavigate()
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const { data, isPending, isError, refetch } = useImageTasks(cursors[cursors.length - 1])
  const items = data?.items
  const { data: services } = useServices()
  const del = useDeleteTask()
  const openItems = useLightboxStore((s) => s.openItems)

  const serviceFor = (task: GalleryTask) =>
    services?.find((service) => service.workflow_id && service.workflow_id === task.workflow_id)

  const rerun = async (task: GalleryTask) => {
    const service = serviceFor(task)
    if (!service) return
    try {
      const detail = await getTask(task.id)
      setOverlay(null)
      navigate(`/services/${service.id}`, { state: { rerunInputs: detail.input_json } })
    } catch {
      useToastStore.getState().add('读取任务参数失败，请重试', 'error')
    }
  }

  const openTask = (task: GalleryTask) => {
    const gallery: LightboxItem[] = []
    let startIndex = 0
    for (const item of items ?? []) {
      if (item.id === task.id) startIndex = gallery.length
      const meta: LightboxMeta = {
        historyTaskId: item.id,
        durationMs: item.duration_ms,
        onRerun: serviceFor(item) ? () => rerun(item) : undefined,
      }
      if (item.video_url) {
        gallery.push({ url: item.video_url, meta, kind: 'video' })
      } else {
        for (const url of item.output_thumbnails) gallery.push({ url, meta })
      }
    }
    openItems(gallery, startIndex)
  }

  const gridRef = useRef<HTMLDivElement>(null)
  const changePage = (next: (string | null)[]) => {
    setCursors(next)
    gridRef.current?.scrollTo({ top: 0 })
  }

  return (
    <div className="absolute inset-0 z-30 flex flex-col" style={{ background: 'var(--bg)', overflow: 'hidden' }}>
      {/* header */}
      <div
        className="shrink-0 flex items-center gap-3"
        style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)' }}
      >
        <h1 className="console-title">历史生成</h1>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{items?.length ?? 0} 条</span>
        <button
          type="button"
          onClick={() => setOverlay(null)}
          aria-label="关闭"
          style={{ marginLeft: 'auto', background: 'transparent', border: 'none', color: 'var(--muted)', cursor: 'pointer' }}
        >
          <X size={18} />
        </button>
      </div>

      {/* grid */}
      <div ref={gridRef} className="console-page flex-1 overflow-auto">
        {isPending ? (
          <div style={{ color: 'var(--muted)', fontSize: 12, textAlign: 'center', padding: 40 }}>加载中…</div>
        ) : isError ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            加载历史失败 <button type="button" onClick={() => refetch()}>重试</button>
          </div>
        ) : !items?.length ? (
          <div style={{ color: 'var(--muted)', fontSize: 12, textAlign: 'center', padding: 60 }}>
            {data?.next_before || cursors.length > 1 ? '这一页没有图像或视频。' : '还没有出图记录。'}
          </div>
        ) : (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
              gap: 14,
            }}
          >
            {items.map((t) => {
              const thumb = t.output_thumbnails[0]
              return (
                <Card
                  key={t.id}
                  task={t}
                  thumb={thumb === t.video_url ? undefined : thumb}
                  isVideo={!!t.video_url}
                  canRerun={!!serviceFor(t)}
                  onOpen={() => openTask(t)}
                  onRerun={() => rerun(t)}
                  onDelete={async () => {
                    if (await confirmDialog({ message: '删除这条出图记录?', danger: true, confirmText: '删除' })) del.mutate(t.id)
                  }}
                />
              )
            })}
          </div>
        )}
        {(cursors.length > 1 || data?.next_before) && (
          <nav aria-label="历史分页" className="flex items-center justify-center gap-3 p-4">
            <button type="button" disabled={cursors.length === 1}
              onClick={() => changePage(cursors.slice(0, -1))}>上一页</button>
            <span>第 {cursors.length} 页</span>
            <button type="button" disabled={!data?.next_before}
              onClick={() => {
                if (data?.next_before) changePage([...cursors, data.next_before])
              }}>下一页</button>
          </nav>
        )}
      </div>
    </div>
  )
}

function Card({
  task, thumb, isVideo, canRerun, onOpen, onRerun, onDelete,
}: {
  task: GalleryTask
  /** 缺省 → 无独立 PNG 缩略(ffmpeg 缺省/抽帧失败),渲染占位视频图标而非 <img>。 */
  thumb: string | undefined
  isVideo: boolean
  canRerun: boolean
  onOpen: () => void
  onRerun: () => void
  onDelete: () => void
}) {
  const [hover, setHover] = useState(false)
  const previewUrl = thumb && /^\/(?:files\/images\/|api\/v1\/tasks\/gallery\/)/.test(thumb)
    ? `${thumb}${thumb.includes('?') ? '&' : '?'}preview=1`
    : thumb
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden',
        background: 'var(--card, var(--bg-accent))',
      }}
    >
      <div style={{ position: 'relative', aspectRatio: '1 / 1', background: 'var(--bg)', cursor: 'zoom-in' }} onClick={onOpen}>
        {thumb ? (
          <img src={previewUrl} alt="" loading="lazy" decoding="async" style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
        ) : (
          <div
            role="img"
            aria-label="视频"
            style={{
              width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
              background: 'var(--card, var(--bg-accent))',
            }}
          >
            <Play size={30} color="var(--muted)" />
          </div>
        )}
        {isVideo && (
          <>
            {thumb && (
              <span
                role="img"
                aria-label="视频"
                style={{
                  position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
                  pointerEvents: 'none',
                }}
              >
                <span style={{
                  width: 40, height: 40, borderRadius: '50%', background: 'rgba(0,0,0,0.55)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                  <Play size={18} color="#fff" fill="#fff" />
                </span>
              </span>
            )}
            <span style={{
              position: 'absolute', bottom: 6, left: 6, fontSize: 10, padding: '1px 6px',
              borderRadius: 8, background: 'rgba(0,0,0,0.6)', color: '#fff',
            }}>
              {task.workflow_name || '视频'}
            </span>
          </>
        )}
        {(task.output_thumbnails?.length ?? 0) > 1 && (
          <span style={{
            position: 'absolute', top: 6, right: 6, fontSize: 10, padding: '1px 6px',
            borderRadius: 8, background: 'rgba(0,0,0,0.6)', color: '#fff',
          }}>
            ×{task.output_thumbnails!.length}
          </span>
        )}
        {hover && (
          <div style={{ position: 'absolute', top: 6, left: 6, display: 'flex', gap: 6 }}>
            {canRerun && (
              <IconAction title="重跑(相同参数)" onClick={(e) => { e.stopPropagation(); onRerun() }}>
                <RefreshCw size={13} />
              </IconAction>
            )}
            <IconAction title="删除" danger onClick={(e) => { e.stopPropagation(); onDelete() }}>
              <Trash2 size={13} />
            </IconAction>
          </div>
        )}
      </div>
      <div style={{ padding: '7px 9px' }}>
        <div style={{
          fontSize: 11, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }} title={task.workflow_name}>
          {task.workflow_name || '—'}
        </div>
        <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 3 }}>
          {new Date(task.created_at).toLocaleString()}
        </div>
      </div>
    </div>
  )
}

function IconAction({
  children, onClick, title, danger,
}: {
  children: React.ReactNode
  onClick: (e: React.MouseEvent) => void
  title: string
  danger?: boolean
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        width: 26, height: 26, borderRadius: 5, border: 'none', cursor: 'pointer',
        background: 'rgba(0,0,0,0.6)', color: danger ? '#f87171' : '#fff',
      }}
    >
      {children}
    </button>
  )
}
