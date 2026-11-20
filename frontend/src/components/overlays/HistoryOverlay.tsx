import { useEffect, useMemo, useRef, useState } from 'react'
import { CalendarDays, ImageOff, Play, RefreshCw, Search, Trash2, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { getTask, useDeleteTask, useImageTasks, type GalleryMedia, type GalleryTask } from '../../api/tasks'
import { useServices } from '../../api/services'
import { confirmDialog } from '../../stores/confirm'
import { useLightboxStore, type LightboxItem } from '../../stores/lightbox'
import { usePanelStore } from '../../stores/panel'
import { useToastStore } from '../../stores/toast'
import './HistoryOverlay.css'

const mediaFilters = [
  { value: 'all', label: '全部' },
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
] as const

const date = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' })
const dateTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
const localMedia = /^\/(?:files\/images\/|api\/v1\/tasks\/gallery\/)/

function getPreviewUrl(task: GalleryTask) {
  const thumb = task.output_thumbnails[0]
  if (!thumb) return undefined
  if (thumb === task.video_url && !localMedia.test(thumb)) return undefined
  return localMedia.test(thumb) ? `${thumb}${thumb.includes('?') ? '&' : '?'}preview=1` : thumb
}

export default function HistoryOverlay() {
  const setOverlay = usePanelStore(s => s.setOverlay)
  const navigate = useNavigate()
  const [media, setMedia] = useState<GalleryMedia>('all')
  const [keyword, setKeyword] = useState('')
  const [query, setQuery] = useState('')
  const [days, setDays] = useState<number | null>(null)
  const gridRef = useRef<HTMLDivElement>(null)
  const moreRef = useRef<HTMLDivElement>(null)
  const { data, isPending, isError, isFetchingNextPage, isFetchNextPageError, hasNextPage,
    fetchNextPage, refetch } = useImageTasks(media, query, days)
  const items = useMemo(() => {
    const byId = new Map<string, GalleryTask>()
    for (const page of data?.pages ?? []) {
      for (const item of page.items) byId.set(item.id, item)
    }
    return Array.from(byId.values())
  }, [data])
  const { data: services } = useServices()
  const del = useDeleteTask()
  const openItems = useLightboxStore(s => s.openItems)

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(keyword.trim()), 230)
    return () => window.clearTimeout(timer)
  }, [keyword])

  useEffect(() => {
    const marker = moreRef.current
    if (!marker || !hasNextPage || isFetchingNextPage || isFetchNextPageError || !('IntersectionObserver' in window)) return
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) void fetchNextPage({ cancelRefetch: false })
    }, { root: gridRef.current, rootMargin: '260px 0px' })
    observer.observe(marker)
    return () => observer.disconnect()
  }, [hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage, data?.pages.length])

  const serviceFor = (task: GalleryTask) =>
    services?.find(service => service.workflow_id && service.workflow_id === task.workflow_id)

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
    for (const item of items) {
      if (item.id === task.id) startIndex = gallery.length
      const meta = {
        historyTaskId: item.id,
        title: item.workflow_name || '生成记录',
        createdAt: item.created_at,
        expiresAt: item.expires_at,
        expired: item.expired,
        durationMs: item.duration_ms,
        poster: item.video_url ? getPreviewUrl(item) : undefined,
        onRerun: serviceFor(item) ? () => rerun(item) : undefined,
      }
      if (item.video_url) gallery.push({ url: item.video_url, meta, kind: 'video' })
      else for (const url of item.output_thumbnails) gallery.push({ url, meta })
    }
    openItems(gallery, startIndex)
  }

  const resetScroll = () => gridRef.current?.scrollTo?.({ top: 0 })

  return (
    <div className="history-gallery absolute inset-0 z-30 flex flex-col">
      <header className="history-header">
        <h1 className="console-title">历史生成</h1>
        <button type="button" className="history-icon" aria-label="关闭历史" onClick={() => setOverlay(null)}>
          <X size={18} />
        </button>
      </header>
      <div className="history-filters">
        <div className="history-filter-group" role="group" aria-label="媒体类型">
          {mediaFilters.map(filter => (
            <button type="button" key={filter.value} aria-pressed={media === filter.value}
              onClick={() => { setMedia(filter.value); resetScroll() }}>{filter.label}</button>
          ))}
        </div>
        <label className="history-search">
          <Search size={16} aria-hidden="true" />
          <input type="search" aria-label="搜索历史" placeholder="搜索工作流、提示词或任务 ID"
            maxLength={120} value={keyword} onChange={event => { setKeyword(event.currentTarget.value); resetScroll() }} />
        </label>
        <label className="history-date-filter">
          <CalendarDays size={16} aria-hidden="true" />
          <select value={days ?? ''} aria-label="生成时间"
            onChange={event => { setDays(Number(event.currentTarget.value) || null); resetScroll() }}>
            <option value="">全部时间</option>
            <option value="7">最近 7 天</option>
            <option value="30">最近 30 天</option>
            <option value="90">最近 90 天</option>
          </select>
        </label>
        {!!items.length && <span className="history-count">已加载 {items.length} 条</span>}
      </div>

      <div ref={gridRef} className="console-page history-scroll">
        {isPending && <p className="history-status" role="status">正在加载历史…</p>}
        {isError && !items.length && !isFetchNextPageError && <div className="history-status">
          加载历史失败 <button type="button" onClick={() => void refetch()}>重试</button>
        </div>}
        {!isPending && !isError && !hasNextPage && !items.length && (
          <p className="history-status">{media !== 'all' || query || days ? '没有符合条件的记录' : '还没有图片或视频记录'}</p>
        )}
        <div className="history-grid">
          {items.map(task => <Card key={task.id} task={task} onOpen={() => openTask(task)}
            onRerun={serviceFor(task) ? () => void rerun(task) : undefined}
            onDelete={async () => {
              if (await confirmDialog({
                message: '删除这条生成记录？清理程序会回收此任务已无其他引用的本地输出。',
                danger: true, confirmText: '删除',
              })) {
                del.mutate(task.id, {
                  onError: () => useToastStore.getState().add('删除历史失败，请重试', 'error'),
                })
              }
            }} />)}
        </div>
        {hasNextPage && <div ref={moreRef} className="history-status">
          <button type="button" disabled={isFetchingNextPage}
            onClick={() => void fetchNextPage({ cancelRefetch: false })}>
            {isFetchingNextPage ? '正在加载…' : isFetchNextPageError ? '重试加载' : '加载更多'}
          </button>
        </div>}
      </div>
    </div>
  )
}

function Card({ task, onOpen, onRerun, onDelete }: {
  task: GalleryTask
  onOpen: () => void
  onRerun?: () => void
  onDelete: () => void
}) {
  const [ratio, setRatio] = useState<number>()
  const [failedPreview, setFailedPreview] = useState<string>()
  const preview = getPreviewUrl(task)
  const isVideo = task.kind === 'video'
  const title = task.workflow_name || '生成记录'
  const expiration = new Date(task.expires_at)
  const count = task.output_thumbnails.length
  return (
    <article className="history-card" style={{ width: `clamp(132px, ${(ratio ?? (isVideo ? 16 / 9 : 4 / 3)) * 210}px, min(500px, 100%))` }}>
      <div className="history-card-visual">
        <button type="button" className="history-card-open" onClick={onOpen} aria-label={`预览${isVideo ? '视频' : '图片'} · ${title}`}>
          {preview && failedPreview !== preview ? (
            <img src={preview} alt="" loading="lazy" decoding="async" onLoad={event => {
              const { naturalWidth, naturalHeight } = event.currentTarget
              if (naturalHeight) setRatio(naturalWidth / naturalHeight)
            }} onError={() => setFailedPreview(preview)} />
          ) : <span className="history-no-preview">
            {isVideo ? <Play size={22} /> : <ImageOff size={22} />}
            {failedPreview ? '封面不可用' : isVideo ? '点击播放' : '预览不可用'}
          </span>}
          {isVideo && preview && failedPreview !== preview && <span className="history-play" aria-hidden="true"><Play size={17} fill="currentColor" /></span>}
        </button>
        <div className="history-card-actions">
          {onRerun && <button type="button" className="history-icon" aria-label="重跑相同参数" title="重跑相同参数" onClick={onRerun}><RefreshCw size={14} /></button>}
          <button type="button" className="history-icon" aria-label="删除记录" title="删除记录" onClick={onDelete}><Trash2 size={14} /></button>
        </div>
        {!isVideo && count > 1 && <span className="history-card-count">×{count}</span>}
      </div>
      <div className="history-card-info">
        <strong title={title}>{title}</strong>
        <time dateTime={task.created_at}>{dateTime.format(new Date(task.created_at))}</time>
        <time className={task.expired ? 'history-expired' : ''} dateTime={task.expires_at} title={dateTime.format(expiration)}>
          {task.expired ? '已到期' : '预计到期'} · {date.format(expiration)}
        </time>
      </div>
    </article>
  )
}
