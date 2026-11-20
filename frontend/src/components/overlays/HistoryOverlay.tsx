import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { Check, ImageOff, ListChecks, Play, RefreshCw, Search, Trash2, X } from 'lucide-react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { getTask, useDeleteGalleryTasks, useGalleryModels, useImageTasks, type GalleryMedia, type GallerySort, type GalleryTask } from '../../api/tasks'
import { useServices } from '../../api/services'
import { confirmDialog } from '../../stores/confirm'
import { useLightboxStore, type LightboxItem } from '../../stores/lightbox'
import { usePanelStore } from '../../stores/panel'
import { useToastStore } from '../../stores/toast'
import { NodeSelectPopover } from '../nodes/NodeSelectPopover'
import './HistoryOverlay.css'

const mediaFilters = [
  { value: 'all', label: '全部' },
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
] as const

const timeFilters = [
  { value: null, label: '全部时间' },
  { value: 7, label: '7 天' },
  { value: 30, label: '30 天' },
  { value: 90, label: '90 天' },
] as const
const selectionLimit = 100
const localMedia = /^\/(?:files\/images\/|api\/v1\/tasks\/gallery\/)/

function previewUrl(url?: string) {
  return url && localMedia.test(url) ? `${url}${url.includes('?') ? '&' : '?'}preview=1` : url
}

function getPreviewUrl(task: GalleryTask) {
  const thumb = task.output_thumbnails[0]
  if (!thumb || (thumb === task.video_url && !localMedia.test(thumb))) return undefined
  return previewUrl(thumb)
}

function keyParameters(task: GalleryTask) {
  const params = task.parameters ?? {}
  const moss = task.model === 'MOSS Image'
  const settings: string[] = []
  if (/^\d+:\d+/.test(moss ? params.moss_aspect_ratio || '' : params.aspect_ratio || '')) {
    settings.push((moss ? params.moss_aspect_ratio : params.aspect_ratio).split(' ')[0])
  } else if (/^\d+$/.test(params.width || '') && /^\d+$/.test(params.height || '')) {
    settings.push(`${params.width}×${params.height}`)
  }
  const pixels = moss ? params.moss_megapixels : params.oldx_megapixels || params.megapixels
  if (/^\d+(?:\.\d+)?$/.test(pixels || '')) settings.push(`${pixels} MP`)
  const steps = moss ? params.moss_steps : params.steps
  if (/^\d+$/.test(steps || '')) settings.push(`${steps} 步`)
  const cfg = moss ? params.moss_cfg : params.oldx_cfg || params.cfg
  if (/^\d+(?:\.\d+)?$/.test(cfg || '')) settings.push(`CFG ${cfg}`)
  if (/^\d+(?:\.\d+)?$/.test(params.fps || '')) settings.push(`${params.fps} FPS`)
  if (/^\d+(?:\.\d+)?$/.test(params.duration || '')) settings.push(`${params.duration} 秒`)
  if (/^\d+$/.test(params.seed || '')) settings.push(`种子 ${params.seed}`)
  return settings
}

export default function HistoryOverlay() {
  const setOverlay = usePanelStore(s => s.setOverlay)
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const tag = searchParams.get('tag')?.slice(0, 32) ?? ''
  const [media, setMedia] = useState<GalleryMedia>('all')
  const [keyword, setKeyword] = useState('')
  const [query, setQuery] = useState('')
  const [days, setDays] = useState<number | null>(null)
  const [model, setModel] = useState('')
  const [sort, setSort] = useState<GallerySort>('newest')
  const { data: models } = useGalleryModels()
  const [selecting, setSelecting] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const clearSelection = () => setSelected(new Set())
  const gridRef = useRef<HTMLDivElement>(null)
  const moreRef = useRef<HTMLDivElement>(null)
  const { data, isPending, isError, isFetchingNextPage, isFetchNextPageError, hasNextPage,
    fetchNextPage, refetch } = useImageTasks(media, query, days, model, sort, tag)
  const items = useMemo(() => {
    const byId = new Map<string, GalleryTask>()
    for (const page of data?.pages ?? []) {
      for (const item of page.items) byId.set(item.id, item)
    }
    return Array.from(byId.values())
  }, [data])
  const { data: services } = useServices()
  const del = useDeleteGalleryTasks()
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
    ?? services?.find(service => service.name === task.workflow_name)

  const rerun = async (task: GalleryTask) => {
    const service = serviceFor(task)
    if (!service) return
    try {
      const detail = await getTask(task.id)
      useLightboxStore.getState().close()
      setOverlay(null)
      navigate(`/services/${service.id}`, { state: { rerunInputs: detail.input_json } })
    } catch {
      useToastStore.getState().add('读取任务参数失败，请重试', 'error')
    }
  }

  const deleteRecords = async (ids: string[], permanent: boolean, fromDetails = false) => {
    if (!ids.length || del.isPending) return
    const action = permanent ? '永久删除' : '删除记录'
    const approved = await confirmDialog({
      title: `${action} ${ids.length} 条历史`,
      message: permanent
        ? '将立即删除记录及没有其他任务引用的本地图片、视频和封面。此操作无法撤销。'
        : '将移除历史记录。本地文件之后由后台清理。',
      danger: true,
      confirmText: action,
    })
    if (!approved) return
    const toasts = useToastStore.getState()
    try {
      const result = await del.mutateAsync({ task_ids: ids, permanent })
      const done = new Set(result.deleted_ids)
      setSelected(previous => new Set([...previous].filter(id => !done.has(id))))
      if (fromDetails && done.size) useLightboxStore.getState().close()
      const summary = [`已删除 ${done.size} 条记录`]
      if (permanent) summary.push(`已清理 ${result.removed_files} 个文件`)
      if (result.shared_files) summary.push(`保留 ${result.shared_files} 个共享文件`)
      if (result.missing_files) summary.push(`${result.missing_files} 个文件已不存在`)
      if (result.skipped) summary.push(`跳过 ${result.skipped} 条不可删除的记录`)
      toasts.add(summary.join('，'), done.size ? 'success' : 'info')
    } catch (error) {
      toasts.add(error instanceof Error ? error.message : '删除历史失败，请重试', 'error')
    }
  }

  const toggleSelection = (id: string) => {
    setSelected(previous => {
      const next = new Set(previous)
      if (next.has(id)) next.delete(id)
      else if (next.size < selectionLimit) next.add(id)
      return next
    })
  }

  const openTask = (task: GalleryTask) => {
    const gallery: LightboxItem[] = []
    let startIndex = 0
    for (const item of items) {
      if (item.id === task.id) startIndex = gallery.length
      const service = serviceFor(item)
      const source = service ? `/services/${service.id}` : item.workflow_id ? `/workflows/${item.workflow_id}` : undefined
      const meta = {
        historyTaskId: item.id,
        title: item.workflow_name || '生成记录',
        model: item.model,
        keyParameters: keyParameters(item),
        createdAt: item.created_at,
        expiresAt: item.expires_at,
        expired: item.expired,
        durationMs: item.duration_ms,
        onRerun: service ? () => rerun(item) : undefined,
        sourceLabel: service ? '打开服务' : '打开工作流',
        onOpenSource: source ? () => {
          useLightboxStore.getState().close()
          setOverlay(null)
          navigate(source)
        } : undefined,
        onTagSearch: (tag: string) => {
          useLightboxStore.getState().close()
          setKeyword('')
          setQuery('')
          setMedia('all')
          setDays(null)
          setModel('')
          setSearchParams({ tag })
          resetFilters()
        },
        onDelete: (permanent: boolean) => void deleteRecords([item.id], permanent, true),
      }
      if (item.video_url) {
        const videos = new Set([item.video_url, ...item.output_thumbnails.filter(url => /\.(mp4|webm|mov|mkv)(?:[?#]|$)/i.test(url))])
        for (const url of videos) {
          const poster = url === item.video_url ? getPreviewUrl(item) : localMedia.test(url) ? previewUrl(url) : undefined
          gallery.push({ url, meta: { ...meta, poster, previewUrl: poster }, kind: 'video' })
        }
      } else {
        for (const url of item.output_thumbnails) gallery.push({ url, meta: { ...meta, previewUrl: previewUrl(url) } })
      }
    }
    openItems(gallery, startIndex)
  }

  const resetFilters = () => {
    clearSelection()
    gridRef.current?.scrollTo?.({ top: 0 })
  }

  return (
    <div className="history-gallery absolute inset-0 z-30 flex flex-col">
      <header className="history-header">
        <h1 className="console-title">图库</h1>
        <button type="button" className="history-manage" aria-pressed={selecting}
          onClick={() => { setSelecting(!selecting); clearSelection() }}>
          <ListChecks size={16} />{selecting ? '退出批量管理' : '批量管理'}
        </button>
        <button type="button" className="history-icon" aria-label="关闭图库" onClick={() => setOverlay(null)}>
          <X size={18} />
        </button>
      </header>
      <div className="history-filters">
        <div className="history-filter-group" role="group" aria-label="媒体类型">
          {mediaFilters.map(filter => (
            <button type="button" key={filter.value} aria-pressed={media === filter.value}
              onClick={() => { setMedia(filter.value); resetFilters() }}>{filter.label}</button>
          ))}
        </div>
        <label className="history-search">
          <Search size={16} aria-hidden="true" />
          <input type="search" aria-label="搜索图库" placeholder="搜索提示词、标签、工作流或任务 ID"
            maxLength={120} value={keyword} onChange={event => { setKeyword(event.currentTarget.value); resetFilters() }} />
        </label>
        <div className="history-date-filter" role="group" aria-label="生成时间">
          {timeFilters.map(filter => (
            <button type="button" key={filter.value ?? 'all'} aria-pressed={days === filter.value}
              onClick={() => { setDays(filter.value); resetFilters() }}>{filter.label}</button>
          ))}
        </div>
        <div className="history-picker">
          <span>模型</span>
          <NodeSelectPopover appearance="console" topLayer ariaLabel="筛选模型" width={140}
            value={model} onChange={value => { setModel(value); resetFilters() }}
            options={[{ value: '', label: '全部模型' }, ...(models ?? []).map(name => ({ value: name, label: name }))]} />
        </div>
        <div className="history-picker">
          <span>排序</span>
          <NodeSelectPopover appearance="console" topLayer ariaLabel="图库排序" width={110}
            value={sort} onChange={value => { setSort(value as GallerySort); resetFilters() }}
            options={[
              { value: 'newest', label: '最新优先' },
              { value: 'oldest', label: '最早优先' },
              { value: 'model', label: '按模型' },
            ]} />
        </div>
        {!!items.length && <span className="history-count">已加载 {items.length} 条</span>}
      </div>

      {tag && <div className="history-active-tag">
        <span>标签</span>
        <button type="button" aria-label={`清除标签 ${tag}`} onClick={() => { setSearchParams({}); resetFilters() }}>
          #{tag} <X size={14} />
        </button>
      </div>}

      {selecting && <div className="history-selection" aria-label="批量操作">
        <strong>{selected.size ? `已选 ${selected.size} 条` : '选择要操作的记录'}</strong>
        <button type="button" disabled={!items.length || del.isPending}
          onClick={() => setSelected(new Set(items.slice(0, selectionLimit).map(item => item.id)))}>
          {items.length > selectionLimit ? '选择前 100 条' : '全选已加载'}
        </button>
        <button type="button" disabled={!selected.size || del.isPending} onClick={clearSelection}>清空选择</button>
        <button type="button" disabled={!selected.size || del.isPending}
          onClick={() => void deleteRecords([...selected], false)}>删除记录</button>
        <button type="button" className="history-danger" disabled={!selected.size || del.isPending}
          onClick={() => void deleteRecords([...selected], true)}>永久删除</button>
      </div>}

      <div ref={gridRef} className="console-page history-scroll">
        {isPending && <p className="history-status" role="status">正在加载历史…</p>}
        {isError && !items.length && !isFetchNextPageError && <div className="history-status">
          加载历史失败 <button type="button" onClick={() => void refetch()}>重试</button>
        </div>}
        {!isPending && !isError && !hasNextPage && !items.length && (
          <p className="history-status">{media !== 'all' || query || days || model || tag ? '没有符合条件的记录' : '还没有图片或视频记录'}</p>
        )}
        <div className="history-grid">
          {items.map((task, index) => (
            <Fragment key={task.id}>
              {sort === 'model' && task.model !== items[index - 1]?.model &&
                <h2 className="history-model-group">{task.model}</h2>}
              <Card task={task} selecting={selecting} selected={selected.has(task.id)}
                selectionFull={selected.size >= selectionLimit}
                onOpen={() => openTask(task)}
                onSelect={() => toggleSelection(task.id)}
                onRerun={serviceFor(task) ? () => void rerun(task) : undefined}
                onDelete={() => void deleteRecords([task.id], false)} />
            </Fragment>
          ))}
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

function Card({ task, selecting, selected, selectionFull, onOpen, onSelect, onRerun, onDelete }: {
  task: GalleryTask
  selecting: boolean
  selected: boolean
  selectionFull: boolean
  onOpen: () => void
  onSelect: () => void
  onRerun?: () => void
  onDelete: () => void
}) {
  const [ratio, setRatio] = useState<number>()
  const [failedPreview, setFailedPreview] = useState<string>()
  const preview = getPreviewUrl(task)
  const isVideo = task.kind === 'video'
  const title = task.workflow_name || '生成记录'
  const count = task.output_thumbnails.length
  const parameters = keyParameters(task).slice(0, 2)
  return (
    <article className="history-card" style={{ width: `clamp(132px, ${(ratio ?? (isVideo ? 16 / 9 : 4 / 3)) * 210}px, min(500px, 100%))` }}>
      <div className="history-card-visual">
        <button type="button" className="history-card-open" onClick={selecting ? onSelect : onOpen}
          disabled={selecting && !selected && selectionFull}
          aria-pressed={selecting ? selected : undefined}
          aria-label={`${selecting ? '选择' : '预览'}${isVideo ? '视频' : '图片'} · ${title}`}>
          {preview && failedPreview !== preview ? (
            <img src={preview} alt="" loading="lazy" decoding="async" onLoad={event => {
              const { naturalWidth, naturalHeight } = event.currentTarget
              if (naturalHeight) setRatio(naturalWidth / naturalHeight)
            }} onError={() => setFailedPreview(preview)} />
          ) : <span className="history-no-preview">
            {isVideo ? <Play size={22} /> : <ImageOff size={22} />}
            {failedPreview ? '封面不可用' : isVideo ? '点击播放' : '预览不可用'}
          </span>}
          {selecting && <span className="history-select-marker" aria-hidden="true">{selected && <Check size={14} />}</span>}
          {isVideo && preview && failedPreview !== preview && <span className="history-play" aria-hidden="true"><Play size={17} fill="currentColor" /></span>}
        </button>
        {!selecting && <div className="history-card-actions">
          {onRerun && <button type="button" className="history-icon" aria-label="重跑相同参数" title="重跑相同参数" onClick={onRerun}><RefreshCw size={14} /></button>}
          <button type="button" className="history-icon" aria-label="删除记录" title="删除记录" onClick={onDelete}><Trash2 size={14} /></button>
        </div>}
        {task.model && <div className="history-card-model" title={[task.model, ...keyParameters(task)].join(' · ')}>
          <strong>{task.model}</strong>
          {!!parameters.length && <span>{parameters.join(' · ')}</span>}
        </div>}
        {!isVideo && count > 1 && <span className="history-card-count">×{count}</span>}
      </div>
    </article>
  )
}
