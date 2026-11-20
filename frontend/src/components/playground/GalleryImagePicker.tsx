import { useEffect, useId, useRef, useState } from 'react'
import { Check, ImageOff, X } from 'lucide-react'
import { useImageTasks } from '../../api/tasks'
import { galleryPreviewUrl } from '../../utils/galleryMedia'
import './GalleryImagePicker.css'

export default function GalleryImagePicker({ destination, onClose, onSelect }: {
  destination: string
  onClose: () => void
  onSelect: (file: Blob) => void
}) {
  const titleId = useId()
  const dialog = useRef<HTMLDialogElement>(null)
  const request = useRef<AbortController | null>(null)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const { data, isPending, isError, refetch, hasNextPage, fetchNextPage, isFetchingNextPage } =
    useImageTasks('image', query, null, '', 'newest', '')
  const images = new Map<string, string>()
  for (const page of data?.pages ?? []) {
    for (const task of page.items) {
      if (task.expired || task.kind !== 'image') continue
      for (const url of task.output_thumbnails) {
        if (/^(?:\/(?!\/)|https?:\/\/)/i.test(url)) images.set(url, task.workflow_name || '图库图片')
      }
    }
  }

  useEffect(() => {
    const modal = dialog.current
    modal?.showModal()
    return () => {
      request.current?.abort()
      modal?.close()
    }
  }, [])

  useEffect(() => {
    const timeout = window.setTimeout(() => setQuery(search.trim()), 230)
    return () => window.clearTimeout(timeout)
  }, [search])

  const importSelectedImage = async () => {
    if (request.current || !images.has(selected)) return
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError('')
    try {
      const response = await fetch(selected, { signal: controller.signal, referrerPolicy: 'no-referrer' })
      if (!response.ok || !response.headers.get('content-type')?.startsWith('image/')) throw new Error('图片无法读取')
      const image = await response.blob()
      if (!controller.signal.aborted) onSelect(image)
    } catch {
      if (!controller.signal.aborted) setError('无法读取原图，请重试或选择其他图片。')
    } finally {
      if (!controller.signal.aborted) {
        request.current = null
        setLoading(false)
      }
    }
  }

  return (
    <dialog ref={dialog} className="qwen-gallery-picker" aria-labelledby={titleId}
      onCancel={event => { event.preventDefault(); onClose() }}>
      <header>
        <div><h2 id={titleId}>从图库选择</h2><p>添加到{destination}</p></div>
        <button type="button" aria-label="关闭图库选择" onClick={onClose}><X size={17} /></button>
      </header>
      <input type="search" aria-label="搜索图库图片" placeholder="搜索提示词、标签、工作流或任务 ID"
        value={search} disabled={loading} onChange={event => { setSelected(''); setSearch(event.currentTarget.value) }}
        onKeyDown={event => { if (event.key === 'Enter') event.preventDefault() }} />
      <div className="qwen-gallery-picker-scroll">
        <div className="qwen-gallery-picker-grid">
          {[...images].map(([url, title], index) => (
            <button type="button" key={url} title={title} aria-label={`${title} · 图片 ${index + 1}`}
              aria-pressed={url === selected} disabled={loading} onClick={() => { setSelected(url); setError('') }}>
              <ImageOff aria-hidden="true" size={18} />
              <img src={galleryPreviewUrl(url)} alt="" decoding="async" loading="lazy"
                onError={event => { event.currentTarget.hidden = true }} />
              {url === selected && <span><Check size={14} /></span>}
            </button>
          ))}
        </div>
        {isPending ? <p role="status">正在加载图片…</p>
          : isError && !images.size ? <button type="button" onClick={() => void refetch()}>重新加载图库</button>
            : !images.size && !hasNextPage ? <p role="status">暂无可选图片</p> : null}
        {hasNextPage && <button type="button" disabled={isFetchingNextPage || loading}
          onClick={() => void fetchNextPage({ cancelRefetch: false })}>
          {isFetchingNextPage ? '加载中…' : '加载更多'}
        </button>}
      </div>
      <footer>
        <span role="status">{error}</span>
        <button type="button" onClick={onClose}>取消</button>
        <button type="button" className="qwen-primary" disabled={loading || !images.has(selected)}
          onClick={() => void importSelectedImage()}>{loading ? '正在读取原图…' : '使用图片'}</button>
      </footer>
    </dialog>
  )
}
