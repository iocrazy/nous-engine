// 历史出图画廊(spec 2026-06-09 run-history PR-B,借鉴 Infinite-Canvas history)。
// 拉最近 task,筛出带 output_thumbnails 的出图记录 → 网格画廊;hover 卡片可「重跑」
// (复用 PR-A:按 workflow_id 找服务跳 Playground 回填 input_json)/「删除」;点图开共享灯箱
// (缩放/平移 + 元信息面板:prompt/参数/时长/重跑;spec 2026-06-10)。
import { useMemo, useState } from 'react'
import { X, RefreshCw, Trash2, Play } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { usePanelStore } from '../../stores/panel'
import { confirmDialog } from '../../stores/confirm'
import { useImageTasks, useDeleteTask, type ExecutionTask } from '../../api/tasks'
import { useServices } from '../../api/services'
import { useLightboxStore, type LightboxItem, type LightboxMeta } from '../../stores/lightbox'

export default function HistoryOverlay() {
  const setOverlay = usePanelStore((s) => s.setOverlay)
  const navigate = useNavigate()
  const { data: tasks, isLoading } = useImageTasks()
  const { data: services } = useServices()
  const del = useDeleteTask()
  const openItems = useLightboxStore((s) => s.openItems)

  const items = useMemo(
    () => (tasks ?? []).filter((t) => (t.output_thumbnails?.length ?? 0) > 0),
    [tasks],
  )

  // 视频产物检测(Task 11):comfy bridge 节点 result 形状是 `{outputs:{node_id:envelope}}`
  // (或旧 fake 测试的 flat `{node_id:envelope}`),envelope 带 video_url 或
  // items[].kind==='video'(见 execution_task_serialize.py _iter_node_outputs/_image_urls)。
  // 卡片缩略图仍走 output_thumbnails[0](ffmpeg 首帧 PNG),灯箱条目则要换成这个视频 url。
  const findVideoUrl = (result: unknown): string | undefined => {
    if (!result || typeof result !== 'object') return undefined
    const r = result as Record<string, unknown>
    const outputsField = r.outputs
    const nodeOutputs = outputsField && typeof outputsField === 'object'
      ? Object.values(outputsField as Record<string, unknown>)
      : Object.values(r)
    for (const v of nodeOutputs) {
      if (!v || typeof v !== 'object') continue
      const vv = v as Record<string, unknown>
      if (typeof vv.video_url === 'string' && vv.video_url) return vv.video_url
      for (const item of (Array.isArray(vv.items) ? vv.items : [])) {
        if (item && typeof item === 'object') {
          const it = item as Record<string, unknown>
          if (it.kind === 'video' && typeof it.url === 'string' && it.url) return it.url
        }
      }
    }
    return undefined
  }

  const canRerun = (t: ExecutionTask) =>
    !!services?.find((s) => !!s.workflow_id && s.workflow_id === t.workflow_id)

  const rerun = (t: ExecutionTask) => {
    const svc = services?.find((s) => !!s.workflow_id && s.workflow_id === t.workflow_id)
    if (!svc) return
    setOverlay(null)
    navigate(`/services/${svc.id}`, { state: { rerunInputs: t.input_json } })
  }

  // 一个 task 的元信息:prompt(input_json 里最长字符串/带 prompt 关键字)+ 其余字段 + 时长 + 重跑。
  const taskMeta = (t: ExecutionTask): LightboxMeta => {
    const inp = (t.input_json ?? {}) as Record<string, unknown>
    const entries = Object.entries(inp).filter(
      ([, v]) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean',
    )
    let prompt: string | undefined
    let promptKey: string | undefined
    for (const [k, v] of entries) {
      if (typeof v === 'string' && (/prompt|提示|描述/i.test(k) || v.length >= 20)) {
        if (!prompt || v.length > prompt.length) { prompt = v; promptKey = k }
      }
    }
    const fields = entries
      .filter(([k]) => k !== promptKey)
      .map(([k, v]) => ({ label: k, value: String(v) }))
    return {
      prompt,
      fields,
      durationMs: (t.duration_ms as number | null | undefined) ?? null,
      onRerun: canRerun(t) ? () => rerun(t) : undefined,
    }
  }

  // 全画廊扁平图集(跨 task 多图)+ 每 task 首图的全局索引(点卡片定位)+ 每 task 检出的
  // 视频 url(Card 判 isVideo/占位缩略同一份数据,免得渲染时再调一遍 findVideoUrl)。
  const { gallery, startIndex, videoUrlByTaskId } = useMemo(() => {
    const g: LightboxItem[] = []
    const start = new Map<string | number, number>()
    const videoMap = new Map<string | number, string>()
    for (const t of items) {
      start.set(t.id, g.length)
      const meta = taskMeta(t)
      const videoUrl = findVideoUrl(t.result)
      if (videoUrl) {
        // 视频任务:灯箱条目是视频 url(不是 PNG 首帧缩略),kind='video' 让 Lightbox
        // 切到 <video> 分支;卡片缩略图仍走 output_thumbnails[0](见下方 Card 调用)。
        videoMap.set(t.id, videoUrl)
        g.push({ url: videoUrl, meta, kind: 'video' })
      } else {
        for (const url of t.output_thumbnails ?? []) g.push({ url, meta })
      }
    }
    return { gallery: g, startIndex: start, videoUrlByTaskId: videoMap }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, services])

  return (
    <div className="absolute inset-0 z-30 flex flex-col" style={{ background: 'var(--bg)', overflow: 'hidden' }}>
      {/* header */}
      <div
        className="shrink-0 flex items-center gap-3"
        style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)' }}
      >
        <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--text)' }}>历史生成</span>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{items.length} 张</span>
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
      <div style={{ flex: 1, overflow: 'auto', padding: 18 }}>
        {isLoading ? (
          <div style={{ color: 'var(--muted)', fontSize: 12, textAlign: 'center', padding: 40 }}>加载中…</div>
        ) : items.length === 0 ? (
          <div style={{ color: 'var(--muted)', fontSize: 12, textAlign: 'center', padding: 60 }}>
            还没有出图记录 — 到服务 Playground 跑一次就会出现在这里。
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
              const thumb = t.output_thumbnails![0]
              const videoUrl = videoUrlByTaskId.get(t.id)
              const isVideo = !!videoUrl
              // ffmpeg 缺省/抽帧失败(thumbnail.py extract_first_frame → None)时
              // output_thumbnails[0] 退化成 video_url 本身(见 _image_urls)—— 这种情况没有
              // 独立 PNG 缩略,不能拿视频文件当 <img src> 渲染(碎图),Card 走占位图标分支。
              const hasDistinctThumb = !isVideo || thumb !== videoUrl
              return (
                <Card
                  key={t.id}
                  task={t}
                  thumb={hasDistinctThumb ? thumb : undefined}
                  isVideo={isVideo}
                  canRerun={canRerun(t)}
                  onOpen={() => openItems(gallery, startIndex.get(t.id) ?? 0)}
                  onRerun={() => rerun(t)}
                  onDelete={async () => {
                    if (await confirmDialog({ message: '删除这条出图记录?', danger: true, confirmText: '删除' })) del.mutate(t.id)
                  }}
                />
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

function Card({
  task, thumb, isVideo, canRerun, onOpen, onRerun, onDelete,
}: {
  task: ExecutionTask
  /** 缺省 → 无独立 PNG 缩略(ffmpeg 缺省/抽帧失败),渲染占位视频图标而非 <img>。 */
  thumb: string | undefined
  isVideo: boolean
  canRerun: boolean
  onOpen: () => void
  onRerun: () => void
  onDelete: () => void
}) {
  const [hover, setHover] = useState(false)
  const summary = task.input_json ? JSON.stringify(task.input_json) : (task.workflow_name || '—')
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
          <img src={thumb} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
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
        }} title={summary}>
          {summary}
        </div>
        <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 3 }}>
          {task.workflow_name} · {new Date(task.created_at).toLocaleString()}
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
