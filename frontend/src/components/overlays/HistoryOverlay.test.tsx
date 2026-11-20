import { beforeEach, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
vi.mock('../../api/tasks', () => ({ useImageTasks, useDeleteGalleryTasks: () => ({ mutateAsync, isPending: false }) }))
vi.mock('../../api/services', () => ({ useServices: () => ({ data: [{ id: '9', workflow_id: '5' }] }) }))
vi.mock('../../stores/confirm', () => ({ confirmDialog: confirm }))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../../stores/panel', () => ({ usePanelStore: (select: (store: { setOverlay: () => void }) => unknown) => select({ setOverlay: vi.fn() }) }))

beforeEach(() => {
  useLightboxStore.setState({ open: false, images: [], metas: [], kinds: [], index: 0 })
  useImageTasks.mockReset().mockReturnValue({ data: { pages: [{ items: [task, video, olderVideo] }] }, hasNextPage: true, fetchNextPage })
  fetchNextPage.mockClear()
  confirm.mockReset()
  mutateAsync.mockReset()
})

it('使用延迟缩略图，打开详情后才加载原图和提示词', () => {
  const { container } = render(<HistoryOverlay />)
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
  fireEvent.change(screen.getByRole('combobox', { name: '生成时间' }), { target: { value: '30' } })
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: ' forest ' } })
  await waitFor(() => expect(useImageTasks).toHaveBeenLastCalledWith('video', 'forest', 30))
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
