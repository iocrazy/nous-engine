import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, act, waitFor, within } from '@testing-library/react'
import Lightbox from './Lightbox'
import { useLightboxStore } from '../../stores/lightbox'

const { galleryFetch } = vi.hoisted(() => ({ galleryFetch: vi.fn() }))
vi.mock('../../api/client', () => ({ apiFetch: galleryFetch }))

const URL1 = 'data:image/png;base64,iVBORw0KGgo='

function scaleOf(transform: string): number {
  const m = transform.match(/scale\(([\d.]+)\)/)
  return m ? parseFloat(m[1]) : 1
}

describe('Lightbox zoom/pan wiring', () => {
  beforeEach(() => {
    act(() => useLightboxStore.setState({ open: false, images: [], metas: [], kinds: [], index: 0 }))
  })

  it('renders meta panel (prompt + fields + rerun) when meta present', () => {
    const onRerun = vi.fn()
    render(<Lightbox />)
    act(() => useLightboxStore.setState({
      open: true, images: [URL1], index: 0,
      metas: [{ prompt: 'a corgi astronaut', fields: [{ label: 'seed', value: '42' }], durationMs: 2000, onRerun }],
    }))
    expect(screen.getByText('a corgi astronaut')).toBeTruthy()
    expect(screen.getByText('seed')).toBeTruthy()
    expect(screen.getByText(/耗时 2\.0s/)).toBeTruthy()
    fireEvent.click(screen.getByText(/重跑/))
    expect(onRerun).toHaveBeenCalled()
  })

  it('no meta panel when meta empty', () => {
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1], metas: [undefined], index: 0 }))
    expect(screen.queryByText(/重跑/)).toBeNull()
  })

  it('下载当前 triggers a download', () => {
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1], metas: [], index: 0 }))
    fireEvent.click(screen.getByTitle('下载当前'))
    expect(clickSpy).toHaveBeenCalledTimes(1)
    clickSpy.mockRestore()
  })

  it('下载全部 triggers one download per image', () => {
    vi.useFakeTimers()
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1, URL1, URL1], metas: [], index: 0 }))
    fireEvent.click(screen.getByTitle(/下载全部/))
    act(() => { vi.advanceTimersByTime(1000) })
    expect(clickSpy).toHaveBeenCalledTimes(3)
    clickSpy.mockRestore()
    vi.useRealTimers()
  })

  it('对比 toggle 显示前后对比视图', () => {
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1, URL1], metas: [], index: 1 }))
    expect(screen.queryByText('当前')).toBeNull()
    fireEvent.click(screen.getByTitle(/前后对比/))
    expect(screen.getByText('当前')).toBeTruthy()
    expect(screen.getByText('对比')).toBeTruthy()
  })

  it('单图带 compareBase(输入/源图)→ 对比按钮出现且标「与输入图」', () => {
    const URL2 = 'data:image/png;base64,SOURCEINPUT='
    render(<Lightbox />)
    act(() => useLightboxStore.setState({
      open: true, images: [URL1], index: 0,
      metas: [{ compareBase: URL2 }],
    }))
    // 单图但有源图基准 → 对比按钮存在(IC 语义:生成图 vs 输入图)
    const btn = screen.getByTitle('前后对比(与输入图)')
    expect(btn).toBeTruthy()
    fireEvent.click(btn)
    expect(screen.getByText('当前')).toBeTruthy()
    expect(screen.getByText('对比')).toBeTruthy()
  })

  it('单图无 compareBase → 对比按钮不出现(纯文生图不误显对比)', () => {
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1], metas: [undefined], index: 0 }))
    expect(screen.queryByTitle(/前后对比/)).toBeNull()
  })

  it('compareBase 等于当前图时忽略(无意义自比)', () => {
    render(<Lightbox />)
    act(() => useLightboxStore.setState({
      open: true, images: [URL1], index: 0, metas: [{ compareBase: URL1 }],
    }))
    expect(screen.queryByTitle(/前后对比/)).toBeNull()
  })

  it('does not render when closed', () => {
    render(<Lightbox />)
    expect(screen.queryByAltText('preview')).toBeNull()
  })

  it('wheel up zooms in; double-click resets', () => {
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1], index: 0 }))
    const img = screen.getByAltText('preview') as HTMLImageElement
    expect(scaleOf(img.style.transform)).toBeCloseTo(1)

    // 滚轮向上 → 放大
    fireEvent.wheel(img.parentElement!, { deltaY: -300, clientX: 700, clientY: 450 })
    expect(scaleOf(img.style.transform)).toBeGreaterThan(1)

    // 双击 → 复位
    fireEvent.dblClick(img)
    expect(scaleOf(img.style.transform)).toBeCloseTo(1)
  })

  it('wheel down past 1 stays clamped at 1', () => {
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1], index: 0 }))
    const img = screen.getByAltText('preview') as HTMLImageElement
    fireEvent.wheel(img.parentElement!, { deltaY: 300, clientX: 700, clientY: 450 })
    expect(scaleOf(img.style.transform)).toBeCloseTo(1)
  })

  it('kind video 渲染 <video> 标签(不是可缩放 img),不显示对比按钮', () => {
    const VIDEO_URL = 'http://x/clip.mp4'
    render(<Lightbox />)
    act(() => useLightboxStore.setState({
      open: true, images: [VIDEO_URL], kinds: ['video'], metas: [undefined], index: 0,
    }))
    const video = document.querySelector('video')
    expect(video).toBeTruthy()
    expect(video?.getAttribute('src')).toBe(VIDEO_URL)
    expect(video?.hasAttribute('controls')).toBe(true)
    expect(video?.hasAttribute('autoplay')).toBe(true)
    expect(screen.queryByAltText('preview')).toBeNull()
    expect(screen.queryByTitle(/前后对比/)).toBeNull()
  })

  it('image 条目回归:kind 缺省仍走原 <img> 缩放分支', () => {
    render(<Lightbox />)
    act(() => useLightboxStore.setState({ open: true, images: [URL1], metas: [undefined], index: 0 }))
    expect(screen.getByAltText('preview')).toBeTruthy()
    expect(document.querySelector('video')).toBeNull()
  })
})


it('图库详情区分任务缩略图和同任务产物，可保存标签并打开服务', async () => {
  const openSource = vi.fn()
  galleryFetch.mockImplementation(async (_url: string, init?: RequestInit) =>
    init?.method === 'PUT' ? { tags: ['训练'] }
      : { prompt: '核心元素：太空服。白色背景。', fields: [{ label: 'seed', value: '42' }], tags: [] })
  render(<Lightbox />)
  act(() => useLightboxStore.getState().openItems([
    { url: '/first.png', meta: { historyTaskId: '101', previewUrl: '/first.png?preview=1', onOpenSource: openSource, sourceLabel: '打开服务' } },
    { url: '/second.png', meta: { historyTaskId: '101', previewUrl: '/second.png?preview=1' } },
    { url: '/third.png', meta: { historyTaskId: '102', previewUrl: '/third.png?preview=1' } },
  ], 0))

  const library = screen.getByRole('navigation', { name: '图库缩略图' })
  const group = screen.getByRole('navigation', { name: '同任务图集' })
  expect(within(library).getAllByRole('button')).toHaveLength(2)
  expect(within(group).getAllByRole('button')).toHaveLength(2)
  expect(await screen.findByText('核心元素', { exact: true })).toBeVisible()
  expect(screen.getByRole('button', { name: '复制提示词' })).toBeVisible()
  fireEvent.change(screen.getByRole('textbox', { name: '新标签' }), { target: { value: '训练' } })
  fireEvent.click(screen.getByRole('button', { name: '添加' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '#训练' })).toBeVisible())
  expect(galleryFetch).toHaveBeenCalledWith('/api/v1/tasks/gallery/101/tags', {
    method: 'PUT', body: JSON.stringify({ tags: ['训练'] }),
  })
  fireEvent.click(screen.getByRole('button', { name: '打开服务' }))
  expect(openSource).toHaveBeenCalledOnce()
  fireEvent.click(within(group).getByRole('button', { name: '查看同任务产物 2' }))
  expect(screen.getByAltText('preview')).toHaveAttribute('src', '/second.png')
  fireEvent.click(within(library).getByRole('button', { name: '预览记录 102' }))
  expect(screen.getByAltText('preview')).toHaveAttribute('src', '/third.png')
  await waitFor(() => expect(screen.queryByRole('navigation', { name: '同任务图集' })).toBeNull())
})


it('缩略图滚动返回 Promise 时仍能关闭图库详情', () => {
  const previous = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  const scroll = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll })
  try {
    galleryFetch.mockResolvedValue({ tags: [] })
    const { unmount } = render(<Lightbox />)
    act(() => useLightboxStore.getState().openItems([
      { url: '/first.png', meta: { historyTaskId: '201' } },
      { url: '/second.png', meta: { historyTaskId: '202' } },
    ], 0))
    expect(scroll).toHaveBeenCalled()
    expect(() => unmount()).not.toThrow()
  } finally {
    if (previous) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', previous)
    else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
  }
})
