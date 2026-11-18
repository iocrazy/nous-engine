import { useCallback, useEffect, useRef, useState, type PointerEvent } from 'react'
import type { SourceDimensions } from './OutpaintFrame'

type Tool = 'brush' | 'erase' | 'rectangle'
type Point = { x: number; y: number }
type Gesture = { pointer: number; start: Point; end: Point; length: number; tool: Tool }
const tools: { id: Tool; label: string }[] = [
  { id: 'brush', label: '画笔' }, { id: 'erase', label: '橡皮擦' }, { id: 'rectangle', label: '选框' },
]

function fillEnclosed(image: ImageData) {
  const { width, height, data } = image
  const outside = new Uint8Array(width * height)
  const queue = new Int32Array(outside.length)
  let tail = 0
  const add = (cell: number) => {
    if (outside[cell] || data[cell * 4 + 3] > 127) return
    outside[cell] = 1
    queue[tail++] = cell
  }
  for (let x = 0; x < width; x++) { add(x); add((height - 1) * width + x) }
  for (let y = 0; y < height; y++) { add(y * width); add(y * width + width - 1) }
  for (let head = 0; head < tail; head++) {
    const cell = queue[head]
    if (cell % width) add(cell - 1)
    if (cell % width < width - 1) add(cell + 1)
    if (cell >= width) add(cell - width)
    if (cell < outside.length - width) add(cell + width)
  }
  for (let cell = 0; cell < outside.length; cell++) {
    if (!outside[cell]) data.set([255, 255, 255, 255], cell * 4)
  }
  return image
}

function snapshot(canvas: HTMLCanvasElement) {
  return canvas.getContext('2d', { willReadFrequently: true })!.getImageData(0, 0, canvas.width, canvas.height)
}

function drawStroke(canvas: HTMLCanvasElement, start: Point, end: Point, width: number, erase: boolean) {
  const context = canvas.getContext('2d')!
  context.save()
  context.globalCompositeOperation = erase ? 'destination-out' : 'source-over'
  context.strokeStyle = '#fff'
  context.lineWidth = width
  context.lineCap = 'round'
  context.beginPath()
  context.moveTo(start.x, start.y)
  context.lineTo(end.x + .001, end.y)
  context.stroke()
  context.restore()
}

export default function ImageMaskDialog({ source, dimensions, existing, onClose, onSave }: {
  source: string
  dimensions: SourceDimensions
  existing?: string
  onClose: () => void
  onSave: (value: string | undefined) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const displayed = useRef<HTMLCanvasElement>(null)
  const bitmap = useRef<HTMLCanvasElement | null>(null)
  const gesture = useRef<Gesture | null>(null)
  const [history, setHistory] = useState<{ frames: ImageData[]; index: number }>({ frames: [], index: 0 })
  const [revision, setRevision] = useState(0)
  const [tool, setTool] = useState<Tool>('brush')
  const [view, setView] = useState<'overlay' | 'binary'>('overlay')
  const [autoFill, setAutoFill] = useState(true)
  const [brush, setBrush] = useState(32)
  const ready = history.frames.length > 0
  const [error, setError] = useState('')
  const ratio = Math.min(1, 1024 / Math.max(dimensions.width, dimensions.height))
  const width = Math.max(1, Math.round(dimensions.width * ratio))
  const height = Math.max(1, Math.round(dimensions.height * ratio))
  const brushWidth = Math.max(1, brush * ratio)
  const refresh = () => setRevision(value => value + 1)

  useEffect(() => {
    const modal = dialog.current
    modal?.showModal()
    return () => modal?.close()
  }, [])

  useEffect(() => {
    let active = true
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d', { willReadFrequently: true })!
    const init = () => {
      if (!active) return
      bitmap.current = canvas
      setHistory({ frames: [snapshot(canvas)], index: 0 })
      setRevision(value => value + 1)
    }
    if (existing) {
      const image = new Image()
      image.onload = () => {
        try {
          context.drawImage(image, 0, 0, width, height)
          const data = snapshot(canvas)
          for (let i = 0; i < data.data.length; i += 4) {
            data.data.set([255, 255, 255, Math.round(data.data[i] * data.data[i + 3] / 255)], i)
          }
          context.putImageData(data, 0, 0)
        } catch {
          setError('无法读取当前遮罩')
        }
        init()
      }
      image.onerror = () => { if (active) setError('无法读取当前遮罩'); init() }
      image.src = existing
    } else init()
    return () => { active = false; bitmap.current = null }
  }, [existing, height, width])

  const filled = useCallback(() => {
    const canvas = bitmap.current
    if (!canvas || !autoFill || gesture.current) return canvas
    const output = document.createElement('canvas')
    output.width = width
    output.height = height
    output.getContext('2d')!.putImageData(fillEnclosed(snapshot(canvas)), 0, 0)
    return output
  }, [autoFill, width, height])

  useEffect(() => {
    const context = displayed.current?.getContext('2d')
    const canvas = filled()
    if (!context || !canvas) return
    context.clearRect(0, 0, width, height)
    context.drawImage(canvas, 0, 0)
    if (view === 'overlay') {
      context.globalCompositeOperation = 'source-in'
      context.fillStyle = '#40e8b19c'
      context.fillRect(0, 0, width, height)
    } else {
      context.globalCompositeOperation = 'destination-over'
      context.fillStyle = '#000'
      context.fillRect(0, 0, width, height)
    }
    context.globalCompositeOperation = 'source-over'
    const action = gesture.current
    if (action?.tool === 'rectangle') {
      context.strokeStyle = '#9cfedb'
      context.strokeRect(action.start.x, action.start.y, action.end.x - action.start.x, action.end.y - action.start.y)
    }
  }, [revision, view, filled, width, height])

  const checkpoint = () => {
    if (!bitmap.current) return
    const next = snapshot(bitmap.current)
    setHistory(previous => {
      const frames = [...previous.frames.slice(0, previous.index + 1), next].slice(-12)
      return { frames, index: frames.length - 1 }
    })
    setError('')
    refresh()
  }

  const changeHistory = (delta: number) => {
    const canvas = bitmap.current
    const next = history.index + delta
    if (!canvas || gesture.current || next < 0 || next >= history.frames.length) return
    canvas.getContext('2d')!.putImageData(history.frames[next], 0, 0)
    setHistory({ ...history, index: next })
    refresh()
  }

  const command = (action: 'clear' | 'invert') => {
    const canvas = bitmap.current
    if (!canvas || gesture.current) return
    const context = canvas.getContext('2d')!
    if (action === 'clear') context.clearRect(0, 0, width, height)
    else {
      const data = snapshot(canvas)
      for (let i = 0; i < data.data.length; i += 4) data.data.set([255, 255, 255, 255 - data.data[i + 3]], i)
      context.putImageData(data, 0, 0)
    }
    checkpoint()
  }

  const point = (event: PointerEvent<HTMLCanvasElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect()
    return {
      x: Math.max(0, Math.min(width, (event.clientX - bounds.left) * width / bounds.width)),
      y: Math.max(0, Math.min(height, (event.clientY - bounds.top) * height / bounds.height)),
    }
  }

  const end = (event: PointerEvent<HTMLCanvasElement>, cancelled = false) => {
    const action = gesture.current
    const canvas = bitmap.current
    if (!canvas || !action || action.pointer !== event.pointerId) return
    gesture.current = null
    const context = canvas.getContext('2d')!
    if (cancelled) {
      context.putImageData(history.frames[history.index], 0, 0)
      refresh()
      return
    }
    if (action.tool === 'rectangle') {
      context.fillStyle = '#fff'
      context.fillRect(action.start.x, action.start.y, action.end.x - action.start.x, action.end.y - action.start.y)
    } else if (action.tool === 'brush' && autoFill) {
      const gap = Math.hypot(action.end.x - action.start.x, action.end.y - action.start.y)
      if (action.length > Math.max(brushWidth * 5, gap * 4) && gap <= Math.max(6, Math.min(brushWidth * 1.65, 60))) {
        drawStroke(canvas, action.end, action.start, brushWidth, false)
      }
    }
    checkpoint()
  }

  const save = () => {
    const canvas = filled()
    if (!canvas || gesture.current) return
    if (!snapshot(canvas).data.some((value, index) => index % 4 === 3 && value > 127)) {
      if (existing) onSave(undefined)
      else setError('请先涂抹或框选区域')
      return
    }
    const output = document.createElement('canvas')
    output.width = dimensions.width
    output.height = dimensions.height
    const context = output.getContext('2d')!
    context.fillStyle = '#000'
    context.fillRect(0, 0, output.width, output.height)
    context.drawImage(canvas, 0, 0, output.width, output.height)
    onSave(output.toDataURL('image/png'))
  }

  return (
    <dialog ref={dialog} className="qwen-mask" aria-label="编辑遮罩" onCancel={event => { event.preventDefault(); onClose() }}
      onKeyDown={event => {
        const key = event.key.toLowerCase()
        if (!(event.ctrlKey || event.metaKey) || !['y', 'z'].includes(key)) return
        event.preventDefault()
        changeHistory(key === 'y' || event.shiftKey ? 1 : -1)
      }}>
      <header><strong>编辑遮罩</strong><button type="button" onClick={onClose}>关闭</button></header>
      <div className="qwen-mask-tools">
        {tools.map(item => <button key={item.id} type="button" aria-pressed={tool === item.id}
          onClick={() => setTool(item.id)}>{item.label}</button>)}
        <label>画笔大小 <input type="range" min={4} max={180} value={brush}
          onChange={event => setBrush(Number(event.currentTarget.value))} /> {brush} px</label>
        <button type="button" aria-pressed={view === 'overlay'} onClick={() => setView('overlay')}>颜色叠加</button>
        <button type="button" aria-pressed={view === 'binary'} onClick={() => setView('binary')}>仅遮罩</button>
        <button type="button" onClick={() => changeHistory(-1)} disabled={!ready || history.index < 1}>撤销</button>
        <button type="button" onClick={() => changeHistory(1)} disabled={!ready || history.index >= history.frames.length - 1}>重做</button>
        <button type="button" onClick={() => command('invert')} disabled={!ready}>反转</button>
        <button type="button" onClick={() => command('clear')} disabled={!ready}>清空</button>
      </div>
      <div className="qwen-mask-stage">
        <div style={{ aspectRatio: `${width} / ${height}`, width: `min(100%, ${54 * width / height}vh)` }}>
          <img src={source} alt="遮罩原图" draggable={false} />
          <canvas ref={displayed} width={width} height={height} aria-label="遮罩绘制画布"
            onPointerDown={event => {
              const canvas = bitmap.current
              if (!canvas || gesture.current || event.button !== 0) return
              event.currentTarget.setPointerCapture(event.pointerId)
              const start = point(event)
              gesture.current = { pointer: event.pointerId, start, end: start, length: 0, tool }
              if (tool !== 'rectangle') drawStroke(canvas, start, start, brushWidth, tool === 'erase')
              refresh()
            }}
            onPointerMove={event => {
              const action = gesture.current
              const canvas = bitmap.current
              if (!canvas || !action || action.pointer !== event.pointerId) return
              const to = point(event)
              if (action.tool !== 'rectangle') drawStroke(canvas, action.end, to, brushWidth, action.tool === 'erase')
              action.length += Math.hypot(to.x - action.end.x, to.y - action.end.y)
              action.end = to
              refresh()
            }}
            onPointerUp={event => end(event)} onPointerCancel={event => end(event, true)}
            onLostPointerCapture={event => end(event, true)} />
        </div>
      </div>
      <footer>
        <label><input type="checkbox" checked={autoFill} onChange={event => setAutoFill(event.target.checked)} /> 自动填充封闭区域</label>
        <span role="status">{error}</span>
        <button className="qwen-primary" type="button" disabled={!ready} onClick={save}>保存遮罩</button>
      </footer>
    </dialog>
  )
}
