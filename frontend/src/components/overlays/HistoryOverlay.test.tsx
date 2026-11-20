import { beforeEach, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useLightboxStore } from '../../stores/lightbox'
import HistoryOverlay from './HistoryOverlay'

const { useImageTasks, fetchNextPage, mutateAsync, confirm } = vi.hoisted(() => ({
  useImageTasks: vi.fn(),
  fetchNextPage: vi.fn(),
  mutateAsync: vi.fn(),
  confirm: vi.fn(),
}))
const task = {
  id: '1', kind: 'image',
  output_thumbnails: ['/files/images/2026-06-10/a.png?token=a&expires=1'],
  model: 'MOSS Image', parameters: { moss_aspect_ratio: '16:9 (HD)', moss_megapixels: '2' },
  video_url: null, workflow_id: '5', workflow_name: 'wf',
  created_at: '2026-06-10T00:00:00Z', expires_at: '2026-09-08T00:00:00Z', duration_ms: 2000,
}
const video = {
  ...task, id: '2', kind: 'video', workflow_name: 'wf-video',
  output_thumbnails: ['http://x/frame.png'], video_url: 'http://x/clip.mp4',
}
const olderVideo = {
  ...video, id: '3', workflow_name: 'older-video',
  output_thumbnails: ['/files/images/2026-06-10/fallback.mp4'],
  video_url: '/files/images/2026-06-10/fallback.mp4',
}
vi.mock('../../api/tasks', () => ({ useImageTasks, useGalleryModels: () => ({ data: ['MOSS Image', 'Qwen Image 2.1'] }), useDeleteGalleryTasks: () => ({ mutateAsync, isPending: false }) }))
vi.mock('../../api/services', () => ({ useServices: () => ({ data: [{ id: '9', workflow_id: '5' }] }) }))
vi.mock('../../stores/confirm', () => ({ confirmDialog: confirm }))
vi.mock('react-router-dom', async () => {
  const { useState } = await import('react')
  return {
    useNavigate: () => vi.fn(),
    useSearchParams: () => {
      const [params, setParams] = useState(() => new URLSearchParams())
      return [params, (values: Record<string, string>) => setParams(new URLSearchParams(values))]
    },
  }
})
vi.mock('../../stores/panel', () => ({ usePanelStore: (select: (store: { setOverlay: () => void }) => unknown) => select({ setOverlay: vi.fn() }) }))

beforeEach(() => {
  useLightboxStore.setState({ open: false, images: [], metas: [], kinds: [], index: 0 })
  useImageTasks.mockReset().mockReturnValue({ data: { pages: [{ items: [task, video, olderVideo] }] }, hasNextPage: true, fetchNextPage })
  fetchNextPage.mockReset()
  confirm.mockReset()
  mutateAsync.mockReset()
})

it('使用延迟缩略图，打开详情后才加载原图和提示词', () => {
  const { container } = render(<HistoryOverlay />)
  expect(screen.getByRole('heading', { name: '图库' })).toBeVisible()
  expect(screen.queryByText('wf')).not.toBeInTheDocument()
  expect(container.querySelector('time')).toBeNull()
  const thumb = container.querySelector('img')
  expect(thumb).toHaveAttribute('src', task.output_thumbnails[0] + '&preview=1')
  expect(thumb).toHaveAttribute('loading', 'lazy')
  fireEvent.click(screen.getByRole('button', { name: '预览图片 · wf' }))
  const state = useLightboxStore.getState()
  expect(state.images).toEqual([task.output_thumbnails[0], video.video_url, olderVideo.video_url])
  expect(state.metas[0]).toMatchObject({ historyTaskId: '1', expiresAt: task.expires_at })
  expect(state.metas[0]?.prompt).toBeUndefined()
  expect(typeof state.metas[0]?.onRerun).toBe('function')
})

it('无封面视频尝试加载首帧，失败仍可播放原视频', () => {
  const { container } = render(<HistoryOverlay />)
  const poster = container.querySelector('img[src$="fallback.mp4?preview=1"]')!
  expect(poster).toBeInTheDocument()
  fireEvent.error(poster)
  expect(screen.getByText('封面不可用')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '预览视频 · older-video' }))
  const state = useLightboxStore.getState()
  expect(state.images[state.index]).toBe(olderVideo.video_url)
  expect(state.kinds[state.index]).toBe('video')
})

it('按媒体、时间和关键词筛选，继续加载时保留当前记录', async () => {
  render(<HistoryOverlay />)
  fireEvent.click(screen.getByRole('button', { name: '视频' }))
  fireEvent.click(screen.getByRole('button', { name: '30 天' }))
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: ' forest ' } })
  await waitFor(() => expect(useImageTasks).toHaveBeenLastCalledWith('video', 'forest', 30, '', 'newest', ''))
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  expect(fetchNextPage).toHaveBeenCalledWith({ cancelRefetch: false })
  expect(screen.getByRole('button', { name: '预览视频 · older-video' })).toBeVisible()
})


it('批量选择可分别提交普通删除和永久删除，并要求确认', async () => {
  render(<HistoryOverlay />)
  fireEvent.click(screen.getByRole('button', { name: '批量管理' }))
  fireEvent.click(screen.getByRole('button', { name: '全选已加载' }))
  expect(screen.getByText('已选 3 条')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '选择视频 · wf-video' }))
  expect(screen.getByText('已选 2 条')).toBeVisible()

  confirm.mockResolvedValueOnce(false)
  fireEvent.click(screen.getByRole('button', { name: '删除记录' }))
  await waitFor(() => expect(confirm).toHaveBeenCalled())
  expect(mutateAsync).not.toHaveBeenCalled()

  confirm.mockResolvedValueOnce(true)
  mutateAsync.mockResolvedValueOnce({ deleted_ids: ['1', '3'], removed_files: 2, missing_files: 0, shared_files: 0, skipped: 0 })
  fireEvent.click(screen.getByRole('button', { name: '永久删除' }))
  await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({ task_ids: ['1', '3'], permanent: true }))
  expect(screen.getByText('选择要操作的记录')).toBeVisible()
})


it('显示缩略图的模型和关键参数，标签从详情跳回精确筛选', () => {
  render(<HistoryOverlay />)
  expect(screen.getAllByText('16:9 · 2 MP')).toHaveLength(3)
  const model = screen.getByRole('combobox', { name: '筛选模型' })
  fireEvent.keyDown(model, { key: 'ArrowDown' })
  expect(screen.getByRole('option', { name: '全部模型', selected: true })).toHaveFocus()
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
  expect(model).toHaveFocus()
  fireEvent.click(model)
  fireEvent.click(screen.getByRole('option', { name: 'MOSS Image' }))
  fireEvent.click(screen.getByRole('combobox', { name: '图库排序' }))
  fireEvent.click(screen.getByRole('option', { name: '按模型' }))
  expect(useImageTasks).toHaveBeenLastCalledWith('all', '', null, 'MOSS Image', 'model', '')
  fireEvent.click(screen.getByRole('button', { name: '预览图片 · wf' }))
  act(() => useLightboxStore.getState().metas[0]?.onTagSearch?.('穿搭'))
  expect(screen.getByRole('button', { name: '清除标签 穿搭' })).toBeVisible()
  expect(useImageTasks).toHaveBeenLastCalledWith('all', '', null, '', 'model', '穿搭')
  fireEvent.click(screen.getByRole('button', { name: '清除标签 穿搭' }))
  expect(useImageTasks).toHaveBeenLastCalledWith('all', '', null, '', 'model', '')
})

it('详情继续加载同一筛选的下一页，保留当前选中项', async () => {
  const next = { ...task, id: '4', output_thumbnails: ['/files/images/2026-06-10/b.png'] }
  fetchNextPage.mockResolvedValue({ data: { pages: [{ items: [task, video, olderVideo] }, { items: [next] }] }, hasNextPage: false })
  render(<HistoryOverlay />)
  fireEvent.click(screen.getByRole('button', { name: '预览视频 · wf-video' }))
  await act(() => useLightboxStore.getState().loadMore?.())
  const state = useLightboxStore.getState()
  expect(fetchNextPage).toHaveBeenCalledWith({ cancelRefetch: false, throwOnError: true })
  expect(state.images).toEqual([task.output_thumbnails[0], video.video_url, olderVideo.video_url, next.output_thumbnails[0]])
  expect(state.index).toBe(1)
  expect(state.loadMore).toBeUndefined()
})
