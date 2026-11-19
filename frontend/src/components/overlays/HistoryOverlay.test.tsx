import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { useLightboxStore } from '../../stores/lightbox'

// mock 数据 hooks + 路由
const task = {
  id: '1',
  output_thumbnails: ['/files/images/2026-06-10/a.png?token=a&expires=1'],
  video_url: null,
  workflow_id: '5',
  workflow_name: 'wf',
  created_at: new Date('2026-06-10T00:00:00Z').toISOString(),
  duration_ms: 2000,
}
const videoTask = {
  id: '2',
  output_thumbnails: ['http://x/frame.png'],
  workflow_id: '5',
  workflow_name: 'wf-video',
  created_at: new Date('2026-06-10T00:00:00Z').toISOString(),
  duration_ms: 4000,
  video_url: 'http://x/clip.mp4',
}
// 视频产物 task,ffmpeg 缺省/首帧抽取失败(thumbnail.py extract_first_frame → None):
// thumbnails 为空,_image_urls 只落 video_url 自己 → output_thumbnails[0] === video_url,
// 没有独立 PNG 缩略。Card 不能拿 mp4 当 <img src> 渲染,要走占位图标分支。
const videoNoThumbTask = {
  id: '3',
  output_thumbnails: ['http://x/clip2.mp4'],
  workflow_id: '6',
  workflow_name: 'wf-video-nothumb',
  created_at: new Date('2026-06-10T00:00:00Z').toISOString(),
  duration_ms: 3000,
  video_url: 'http://x/clip2.mp4',
}
vi.mock('../../api/tasks', () => ({
  useImageTasks: () => ({ data: { items: [task, videoTask, videoNoThumbTask], next_before: null } }),
  useDeleteTask: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../api/services', () => ({
  useServices: () => ({ data: [{ id: 9, workflow_id: '5', name: 'svc' }] }),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
// panel store 模块加载时读 localStorage(Node24 测试环境 localStorage 异常)→ mock 绕开。
vi.mock('../../stores/panel', () => ({ usePanelStore: (sel: (s: { setOverlay: () => void }) => unknown) => sel({ setOverlay: () => {} }) }))

import HistoryOverlay from './HistoryOverlay'

describe('HistoryOverlay → 共享灯箱', () => {
  beforeEach(() => { useLightboxStore.setState({ open: false, images: [], metas: [], kinds: [], index: 0 }) })

  it('点卡片用 openItems 打开共享灯箱,只传任务 ID 和重跑动作，由灯箱按需获取元信息', () => {
    const { container } = render(<HistoryOverlay />)
    const img = container.querySelector('img')!
    expect(img.getAttribute('src')).toBe('/files/images/2026-06-10/a.png?token=a&expires=1&preview=1')
    expect(img.getAttribute('loading')).toBe('lazy')
    fireEvent.click(img.parentElement!)
    const st = useLightboxStore.getState()
    expect(st.open).toBe(true)
    // 画廊是跨 task 的扁平图集(见组件注释),第二、三个 task 是视频 → 追加视频条目。
    expect(st.images).toEqual([task.output_thumbnails[0], 'http://x/clip.mp4', 'http://x/clip2.mp4'])
    expect(st.metas[0]?.historyTaskId).toBe('1')
    expect(st.metas[0]?.prompt).toBeUndefined()
    expect(typeof st.metas[0]?.onRerun).toBe('function') // 有对应服务 → 可重跑
  })

  it('视频任务卡片渲染 ▶ 角标 + 工作流名标签,点开传给灯箱的是视频 url + kind video', () => {
    const { container, getAllByLabelText } = render(<HistoryOverlay />)
    // ▶ 角标(aria-label="视频")存在(videoTask 的 overlay + videoNoThumbTask 的占位,各一个)
    expect(getAllByLabelText('视频').length).toBe(2)
    // 服务/工作流名标签
    expect(container.textContent).toContain('wf-video')

    const imgs = container.querySelectorAll('img')
    // 只有 task(纯图)+ videoTask(有独立 PNG 缩略)渲染 <img>;videoNoThumbTask 无。
    expect(imgs.length).toBe(2)
    const videoCardThumb = imgs[1] // 第二张卡片 = videoTask,缩略图仍是 PNG 首帧
    expect(videoCardThumb.getAttribute('src')).toBe('http://x/frame.png')

    fireEvent.click(videoCardThumb.parentElement!)
    const st = useLightboxStore.getState()
    expect(st.open).toBe(true)
    // 灯箱条目是视频 url(不是缩略 PNG),kind 标记为 video
    expect(st.images).toContain('http://x/clip.mp4')
    const idx = st.images.indexOf('http://x/clip.mp4')
    expect(st.kinds[idx]).toBe('video')
  })

  it('无独立缩略的视频任务(ffmpeg 抽帧失败):不渲染 <img src=mp4>,占位图标兜底,点击仍开对视频', () => {
    const { container, getAllByLabelText } = render(<HistoryOverlay />)
    // 没有任何 <img> 的 src 指向 mp4(碎图 bug 的回归断言)
    expect(container.querySelector('img[src*=".mp4"]')).toBeNull()
    expect(container.querySelector('img[src="http://x/clip2.mp4"]')).toBeNull()

    // 第二个「视频」aria-label 元素是占位图标本体(无 thumb → 直接是占位 div,不是 overlay span)
    const badges = getAllByLabelText('视频')
    expect(badges.length).toBe(2)
    const placeholder = badges[1]
    expect(placeholder.tagName).toBe('DIV')

    fireEvent.click(placeholder.parentElement!)
    const st = useLightboxStore.getState()
    expect(st.open).toBe(true)
    const idx = st.images.indexOf('http://x/clip2.mp4')
    expect(idx).toBeGreaterThanOrEqual(0)
    expect(st.kinds[idx]).toBe('video')
  })
})
