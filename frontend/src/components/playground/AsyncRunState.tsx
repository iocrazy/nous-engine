import { useEffect, useRef, useState } from 'react'
import { Ban, Check, LoaderCircle, XCircle } from 'lucide-react'
import { cancelPrediction, getPrediction, type Prediction } from '../../api/predictions'

export interface AsyncRunStateProps {
  predictionId: string
  compact?: boolean
  /** succeeded 终态触发一次,带上 prediction.output(供调用方直接喂给 SchemaDrivenOutput)。 */
  onDone: (output: Record<string, unknown> | null) => void
  /** failed 终态触发一次,带上 prediction.error —— 调用方靠这个回调解锁(复位 running/
   *  submit 按钮),不只是靠 onDone;否则 failed 会永久锁死 Playground(无路径复位)。 */
  onError: (message: string) => void
  /** canceled 终态触发一次 —— 不管是本组件「取消」按钮触发,还是轮询探测到的外部取消
   *  (另一个客户端 / 管理操作取消了同一个 prediction),调用方都要解锁。 */
  onCancel: () => void
}

const POLL_MS = 2000

const TERMINAL: ReadonlySet<Prediction['status']> = new Set(['succeeded', 'failed', 'canceled'])

// 四段进度条(spec 2026-08-10 task-10):已提交(mount 即达成)→ 排队中(starting)→
// 运行中(processing,带耗时秒表)→ 完成(终态,succeeded/failed/canceled 三种落地文案)。
const STAGE_LABELS = ['已提交', '排队中', '运行中', '完成'] as const

function stageIndex(status: Prediction['status'] | undefined): number {
  switch (status) {
    case 'starting': return 1
    case 'processing': return 2
    case 'succeeded':
    case 'failed':
    case 'canceled':
      return 3
    default:
      return 0
  }
}

function tickElapsed(sinceIso: string | null, now: number): string {
  if (!sinceIso) return '0:00'
  const start = new Date(sinceIso).getTime()
  const sec = Math.max(0, Math.floor((now - start) / 1000))
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

/** Playground 异步运行态(comfy_template respond-async 服务):2s 轮询 prediction 状态,
 * 渲染分段进度 + 取消按钮;三个终态各走各的回调结算一次(succeeded→onDone,failed→onError,
 * canceled→onCancel),不管终态是轮询探测到的还是本组件「取消」按钮请求直接返回的 —— 调用方
 * 靠这些回调解锁自己的 running 态,任何终态都不能只停在本组件内部没个出口(会永久锁死)。 */
export default function AsyncRunState({ predictionId, compact = false, onDone, onError, onCancel }: AsyncRunStateProps) {
  const [prediction, setPrediction] = useState<Prediction | null>(null)
  const [pollError, setPollError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const status = prediction?.status
  // 三个终态回调只该二选一地各触发一次——不管是轮询循环发现的,还是 cancel 请求直接
  // 返回的(二者可能对同一个 prediction 竞态命中,只认第一个)。
  const settledRef = useRef(false)

  const settle = (pred: Prediction) => {
    if (settledRef.current) return
    if (pred.status === 'succeeded') {
      settledRef.current = true
      onDone(pred.output)
    } else if (pred.status === 'failed') {
      settledRef.current = true
      onError(pred.error ?? '未知错误')
    } else if (pred.status === 'canceled') {
      settledRef.current = true
      onCancel()
    }
  }

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      try {
        const pred = await getPrediction(predictionId)
        if (!alive) return
        setPollError(null)
        setPrediction(pred)
        if (TERMINAL.has(pred.status)) {
          settle(pred)
          return
        }
      } catch (e) {
        // 轮询偶发网络抖动:留住上一帧,不中断轮询,下一轮重试。
        if (alive) setPollError((e as Error).message ?? String(e))
      }
      if (alive && !settledRef.current) timer = setTimeout(poll, compact ? 1000 : POLL_MS)
    }

    poll()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
    // predictionId 变化才重开一轮轮询;回调由调用方每次渲染新建也不该重触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [predictionId, compact])

  // 运行中耗时秒表:仅在未到终态时每秒 tick。
  useEffect(() => {
    if (!status || TERMINAL.has(status)) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [status])

  const handleCancel = async () => {
    setCancelling(true)
    try {
      // 后端 cancel_prediction 有 TOCTOU 守护:interrupt 等待窗口内渲染可能已经跑完,
      // 这时返回的是 succeeded+output,不是 cancelled —— 照实结算(settle 按 pred.status
      // 分派),不能无条件当成「已取消」把刚拿到的结果丢掉。
      const pred = await cancelPrediction(predictionId)
      setPrediction(pred)
      if (TERMINAL.has(pred.status)) settle(pred)
    } catch (e) {
      setPollError((e as Error).message ?? String(e))
    } finally {
      setCancelling(false)
    }
  }

  const idx = stageIndex(status)
  const isTerminal = status !== undefined && TERMINAL.has(status)
  const progress = prediction?.progress
  const render = progress?.comfy
  const currentStep = render?.step
  const totalSteps = render?.total
  if (compact) {
    const phase = render?.phase === 'running' ? (render.label ?? '生成中')
      : render?.phase === 'queued' || status === 'starting' ? '等待 ComfyUI'
        : status === 'processing' ? '准备任务' : '已提交'
    return <div className="qwen-run-status">
      <span role="status">{phase}{prediction?.created_at && ` · ${tickElapsed(prediction.created_at, now)}`}
        {currentStep !== undefined && totalSteps !== undefined && ` · ${currentStep}/${totalSteps} 步`}
      </span>
      {currentStep !== undefined && totalSteps !== undefined &&
        <progress aria-label="当前节点进度" value={currentStep} max={totalSteps} />}
      {pollError && <small role="status">进度更新失败，正在重试</small>}
      {!isTerminal && <button type="button" disabled={cancelling} onClick={handleCancel}>
        {cancelling ? '正在取消…' : '取消任务'}
      </button>}
    </div>
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '14px 18px' }}>
      <style>{`@keyframes asyncRunSpin{to{transform:rotate(360deg)}}`}</style>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {STAGE_LABELS.map((label, i) => {
          const passed = i < idx
          const current = i === idx
          const isFinalRow = i === 3
          let dotColor = 'var(--muted)'
          if (isFinalRow && current) {
            dotColor = status === 'failed' ? 'var(--error, #ef4444)'
              : status === 'canceled' ? 'var(--muted)'
              : 'var(--ok, #34c759)'
          } else if (passed || (current && !isFinalRow)) {
            dotColor = current ? 'var(--accent)' : 'var(--ok, #34c759)'
          }
          const rowLabel = isFinalRow && current
            ? (status === 'failed' ? '失败' : status === 'canceled' ? '已取消' : '完成')
            : label
          return (
            <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
              <span style={{
                width: 16, height: 16, borderRadius: '50%', flexShrink: 0,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: passed || (current && isFinalRow) ? dotColor : 'transparent',
                border: `1.5px solid ${dotColor}`,
                color: '#fff',
              }}>
                {passed ? (
                  <Check size={10} />
                ) : current && !isFinalRow ? (
                  <LoaderCircle size={10} style={{ animation: 'asyncRunSpin 1s linear infinite', color: dotColor }} />
                ) : current && isFinalRow && status === 'failed' ? (
                  <XCircle size={11} style={{ color: dotColor }} />
                ) : current && isFinalRow && status === 'canceled' ? (
                  <Ban size={10} style={{ color: dotColor }} />
                ) : current && isFinalRow ? (
                  <Check size={10} />
                ) : null}
              </span>
              <span style={{
                color: current ? 'var(--text)' : passed ? 'var(--text)' : 'var(--muted)',
                fontWeight: current ? 600 : 400,
              }}>
                {rowLabel}
              </span>
              {current && !isFinalRow && (
                <span style={{ color: 'var(--muted)', fontFamily: 'var(--mono, monospace)', fontSize: 11 }}>
                  {status === 'processing' ? tickElapsed(prediction?.created_at ?? null, now) : ''}
                  {status === 'processing' && progress && progress.nodes_total > 0
                    ? ` · ${progress.nodes_done}/${progress.nodes_total} 节点`
                    : ''}
                </span>
              )}
            </div>
          )
        })}
      </div>

      {status === 'failed' && prediction?.error && (
        <div style={{ color: 'var(--error, #ef4444)', fontSize: 12, lineHeight: 1.5 }}>
          {prediction.error}
        </div>
      )}
      {pollError && (
        <div style={{ color: 'var(--warn, #f59e0b)', fontSize: 11 }}>
          轮询异常(将自动重试):{pollError}
        </div>
      )}

      {!isTerminal && (
        <button
          type="button"
          onClick={handleCancel}
          disabled={cancelling}
          style={{
            alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '5px 12px', borderRadius: 6, fontSize: 12,
            background: 'transparent', color: 'var(--error, #ef4444)',
            border: '1px solid var(--border)', cursor: cancelling ? 'not-allowed' : 'pointer',
            opacity: cancelling ? 0.6 : 1,
          }}
        >
          <Ban size={12} />
          {cancelling ? '取消中…' : '取消'}
        </button>
      )}
    </div>
  )
}
