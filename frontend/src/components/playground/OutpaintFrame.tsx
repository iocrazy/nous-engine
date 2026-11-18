import { useRef, useState, type PointerEvent } from 'react'
import { Lock, LockOpen } from 'lucide-react'

const edges = ['left', 'right', 'top', 'bottom'] as const
type Edge = typeof edges[number]
type Axis = 'width' | 'height'
export type OutpaintValues = Record<Edge, number>
export interface SourceDimensions { width: number; height: number }

const axisEdges = { width: ['left', 'right'], height: ['top', 'bottom'] } as const
const edgeAxis: Record<Edge, Axis> = { left: 'width', right: 'width', top: 'height', bottom: 'height' }
const ratios = { '1:1': 1, '4:3': 4 / 3, '3:2': 3 / 2, '16:9': 16 / 9, '9:16': 9 / 16 }
const labels = { left: '左', right: '右', top: '上', bottom: '下', width: '宽度', height: '高度' }
const directions: { position: string; edges: Edge[] }[] = [
  {position:'nw', edges:['left','top']}, {position:'n', edges:['top']},
  {position:'ne', edges:['right','top']}, {position:'e', edges:['right']},
  {position:'se', edges:['right','bottom']}, {position:'s', edges:['bottom']},
  {position:'sw', edges:['left','bottom']}, {position:'w', edges:['left']},
]

function clamp(value: number, max: number) {
  return Math.max(0, Math.min(Math.round(value), max))
}

function padsFor(dimensions: SourceDimensions, values: OutpaintValues) {
  return Object.fromEntries(edges.map(edge =>
    [edge, Math.trunc(dimensions[edgeAxis[edge]] * values[edge] / 100)])) as OutpaintValues
}

export default function OutpaintFrame({ image, dimensions, values, limits, onChange, disabled }: {
  image: string
  dimensions: SourceDimensions
  values: OutpaintValues
  limits: OutpaintValues
  onChange: (values: OutpaintValues) => void
  disabled: boolean
}) {
  const [locked, setLocked] = useState({ width: false, height: false })
  const frameRef = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x: number; y: number; scale: number; values: OutpaintValues } | null>(null)
  const [dragFrame, setDragFrame] = useState<{ width: number; height: number; scale: number; left: number; top: number } | null>(null)
  const pads = padsFor(dimensions, values)
  const width = dimensions.width + pads.left + pads.right
  const height = dimensions.height + pads.top + pads.bottom
  const outputScale = Math.min(1, 5120 / Math.max(width, height))

  const adjust = (chosen: Edge[], x: number, y: number, original: OutpaintValues) => {
    const next = { ...original }
    for (const edge of chosen) {
      const axis = edgeAxis[edge]
      if (locked[axis]) continue
      const delta = (axis === 'width' ? x : y) * (edge === 'left' || edge === 'top' ? -1 : 1)
      next[edge] = clamp(original[edge] + 100 * delta / dimensions[axis], limits[edge])
    }
    onChange(next)
  }

  const changeDimension = (axis: Axis, target: number) => {
    if (!Number.isFinite(target)) return
    const [first, second] = axisEdges[axis]
    const total = clamp((target / dimensions[axis] - 1) * 100, limits[first] + limits[second])
    const previous = values[first] + values[second]
    const nextFirst = Math.max(total - limits[second], clamp(total * (previous ? values[first] / previous : 0.5), limits[first]))
    onChange({ ...values, [first]: nextFirst, [second]: total - nextFirst })
  }

  const begin = (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || !frameRef.current) return
    const rect = frameRef.current.getBoundingClientRect()
    const scale = rect.width / width
    if (!scale) return
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { x: event.clientX, y: event.clientY, scale, values }
    setDragFrame({ width, height, scale, left: pads.left, top: pads.top })
  }

  const end = () => {
    drag.current = null
    setDragFrame(null)
  }

  return (
    <div className="qwen-outpaint">
      <div className="qwen-outpaint-controls">
        {(Object.keys(axisEdges) as Axis[]).map(axis => {
          const size = axis === 'width' ? width : height
          return (
            <div key={axis}>
              <label>{labels[axis]}（px）
                <input key={size} type="number" min={dimensions[axis]} defaultValue={size} disabled={disabled || locked[axis]}
                  onBlur={event => { changeDimension(axis, event.currentTarget.value === '' ? NaN : Number(event.currentTarget.value)); event.currentTarget.value = String(size) }}
                  onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur() } }} />
              </label>
              <button type="button" aria-label={`锁定${labels[axis]}`} aria-pressed={locked[axis]} disabled={disabled}
                onClick={() => setLocked(previous => ({ ...previous, [axis]: !previous[axis] }))}>
                {locked[axis] ? <Lock size={13} /> : <LockOpen size={13} />}
              </button>
            </div>
          )
        })}
        <label className="qwen-extent-ratio">画幅预设
          <select value="" disabled={disabled || (locked.width && locked.height)} onChange={event => {
            const ratio = ratios[event.currentTarget.value as keyof typeof ratios]
            if (!ratio) return
            const axis = locked.width || (!locked.height && width / height >= ratio) ? 'height' : 'width'
            changeDimension(axis, axis === 'width' ? height * ratio : width / ratio)
          }}>
            <option value="">自由</option>
            {Object.keys(ratios).map(ratio => <option key={ratio} value={ratio}>{ratio}</option>)}
          </select>
        </label>
      </div>
      <div className="qwen-extent-stage">
        <div ref={frameRef} className="qwen-extent-frame" style={{
          width: dragFrame ? `${width * dragFrame.scale}px` : `min(100%, ${280 * width / height}px)`,
          height: dragFrame ? `${height * dragFrame.scale}px` : undefined,
          transform: dragFrame ? `translate(${((width - dragFrame.width) / 2 - pads.left + dragFrame.left) * dragFrame.scale}px, ${((height - dragFrame.height) / 2 - pads.top + dragFrame.top) * dragFrame.scale}px)` : undefined,
          aspectRatio: `${width} / ${height}`,
        }}>
          <img src={image} draggable={false} alt="扩图原图" style={{
            left: `${pads.left / width * 100}%`, top: `${pads.top / height * 100}%`,
            width: `${dimensions.width / width * 100}%`, height: `${dimensions.height / height * 100}%`,
          }} />
          {directions.map(direction => (
            <button key={direction.position} className={`qwen-extent-handle ${direction.position}`} type="button"
              aria-label={`拖动${direction.edges.map(edge => labels[edge]).join('')}调整扩图`}
              disabled={disabled || direction.edges.every(edge => locked[edgeAxis[edge]])}
              onPointerDown={begin} onPointerMove={event => {
                if (!drag.current || !event.currentTarget.hasPointerCapture(event.pointerId)) return
                const { x, y, scale, values: initial } = drag.current
                adjust(direction.edges, (event.clientX - x) / scale, (event.clientY - y) / scale, initial)
              }} onPointerUp={end} onLostPointerCapture={end} onPointerCancel={end}
              onKeyDown={event => {
                const delta = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key]
                if (!delta) return
                event.preventDefault()
                adjust(direction.edges, delta[0] * dimensions.width / 100, delta[1] * dimensions.height / 100, values)
              }} />
          ))}
        </div>
      </div>
      <small>预计画布 {Math.round(width * outputScale)} × {Math.round(height * outputScale)} px · 按工作流百分比步进</small>
    </div>
  )
}
