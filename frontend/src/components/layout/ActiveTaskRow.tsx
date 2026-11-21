/**
 * ActiveTaskRow — 任务面板重置 PR-3b:活动任务行(对齐 mockup `.task-row`)。
 *
 *   ┌── timeline-node ──┐   ┌── task-card.active ──────────────────────────┐
 *   │  32×32 圆 + icon  │   │ [IMAGE chip] flux2-klein-bf16  [RUNNING chip]│
 *   │  running glow     │   │ 1024×1024 · seed 42 · cfg 4.5                │
 *   └───────────────────┘   │ ⚡ dit denoise · step 27/50 · 240ms · ETA 5.5s│
 *                           └──────────────────────────────────────────────┘
 *
 * type icon 映射:image → Image、tts → Mic、llm → MessageSquare、vision → Eye。
 */
import { Image as ImageIcon, Mic, MessageSquare, Eye, Captions, Film, Zap, Activity } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ExecutionTask } from '../../api/tasks'
import { useTaskProgressStore } from '../../stores/taskProgress'
import { comfyNodeProgress } from '../../utils/comfyProgress'

type TaskType = 'image' | 'tts' | 'vision' | 'llm' | 'asr' | 'video'

/** RUNNING 任务卡实时运行计时:active 时每秒 tick 一次 Date.now() 触发 re-render。
 * 非 active 不起 interval(完成后停在终值,由 duration_ms 接管展示)。 */
function useNowTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [active])
  return now
}

/** elapsed ms → 人读:<60s 「8s」;≥60s 「1m23s」。tabular-nums 防跳动。 */
function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

const TYPE_LABEL: Record<TaskType, string> = {
  image: 'IMAGE', tts: 'TTS', vision: 'VISION', llm: 'LLM', asr: '语音识别',
  video: 'VIDEO',
}

const STAGE_LABEL: Record<string, string> = {
  model_load: '加载模型',
  text_encode: 'text encode',
  dit_denoise: 'dit denoise',
  vae_decode: 'vae decode',
  tts_synth: 'tts synth',
  llm_gen: 'llm gen',
  vision_inference: 'vision inference',
}

function getTaskType(t: ExecutionTask): TaskType | null {
  const v = (t as ExecutionTask & { type?: string }).type ?? t.task_type
  return (
    v === 'image' || v === 'tts' || v === 'vision' || v === 'llm' ||
    v === 'asr' || v === 'video'
  ) ? v : null
}

// 把 icon 选择写成直接返回 JSX,避免「render 期间创建组件 ref」eslint 报错
// (react-x/no-context-provider 类规则会把函数返回组件 ref 再用作 JSX 元素当作创建组件)。
function renderTypeIcon(type: TaskType | null) {
  const props = { size: 14, strokeWidth: 1.8 } as const
  switch (type) {
    case 'image': return <ImageIcon {...props} />
    case 'tts': return <Mic {...props} />
    case 'llm': return <MessageSquare {...props} />
    case 'vision': return <Eye {...props} />
    case 'asr': return <Captions {...props} />
    case 'video': return <Film {...props} />
    default: return <Activity {...props} />
  }
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

/** 整体进度(对齐 ComfyUI「总进度」):跨 stage 加权成单条 0-100%。denoise 是耗时主体
 * → 占 5-95%(按 step 推进);model_load 是不定态(返回 null → 进度条走滑动动画,不造假 %)。
 * 这是工作流**总**进度;画布各节点自己的进度(KSampler 步进条等)是**每节点**进度,两层并存。 */
function overallPercent(
  stage: string | null,
  step: { done: number; total: number } | null,
): number | null {
  if (!stage) return null
  const frac = step && step.total > 0 ? step.done / step.total : 0
  switch (stage) {
    case 'model_load': return null // 不定态(加载中,无可测百分比)
    case 'text_encode': return 5
    case 'dit_denoise': return Math.min(95, Math.round(5 + 90 * frac))
    case 'vae_decode': return 97
    default: return null
  }
}

function getMetaLine(t: ExecutionTask): string {
  // 通用 meta 行 — 优先展示尺寸/seed/cfg(image),其它 service 用 workflow_id 等做兜底。
  const w = t.image_width, h = t.image_height
  const parts: string[] = []
  if (w && h) parts.push(`${w}×${h}`)
  // workflow_id / nodes 信息略,后续 PR-3c 详情卡再展开。
  if (parts.length === 0) {
    parts.push(`#${t.id}`)
  }
  return parts.join(' · ')
}

export default function ActiveTaskRow({ task }: { task: ExecutionTask }) {
  const type = getTaskType(task)
  const isRunning = task.status === 'running'

  // 实时运行计时(用户要的「边跑边显示已用时」)。created_at 为起点 —— 单用户即时跑
  // 时 ≈ 开始时刻(含排队的极短间隔)。完成后停 interval,duration_ms 接管最终耗时。
  const now = useNowTick(isRunning)
  const elapsedMs = isRunning ? now - new Date(task.created_at).getTime() : null

  // PR-6:全局 progress map(/ws/tasks `event: "progress"`)按 task.id 取 L3 数据 ——
  // 不再依赖 useExecutionStore.taskId 匹配,**多任务并发场景每行都有自己的 callout**。
  const progress = useTaskProgressStore((s) => s.byTaskId.get(task.id))
  const stage = progress?.stage ?? null
  const step = progress?.step != null && progress.totalSteps != null
    ? { done: progress.step, total: progress.totalSteps } : null
  const stepLatencyMs = progress?.stepLatencyMs ?? null
  const etaMs = progress?.etaMs ?? null

  // Bug 2:model_load 是不定态阶段(加载模型,无 step / 不造假百分比)—— 也要显示 callout
  // (「加载模型中…」),否则加载期间 RUNNING 卡一片空白。其余阶段仍要求 step 才显示。
  const isIndeterminate = stage === 'model_load'
  const showL3 = isRunning && stage != null && (step != null || isIndeterminate)

  // 整体进度条(ComfyUI「总进度」):running 全程显示。determinate(有 % → 绿条按比例填充)
  // 或 indeterminate(model_load / 还没出 stage → 滑动动画,不造假 %)。
  const comfy = progress?.comfy ?? task.comfy_progress
  const comfyNode = comfy ? comfyNodeProgress(comfy) : undefined
  const displayedPct = comfyNode ? comfyNode.steps?.percent : overallPercent(stage, step)

  return (
    <div className="relative flex gap-2.5">
      {/* timeline-node */}
      <div
        className="flex items-center justify-center shrink-0 rounded-full"
        style={{
          width: 32, height: 32,
          border: `2px solid ${isRunning ? 'var(--status-running)' : 'var(--tp-border-strong)'}`,
          background: 'var(--tp-bg-panel)',
          color: isRunning ? 'var(--status-running)' : 'var(--tp-text-muted)',
          boxShadow: isRunning
            ? '0 0 14px rgba(74, 222, 128, 0.55), inset 0 0 8px rgba(74, 222, 128, 0.18)'
            : undefined,
        }}
        aria-label={`task type ${type ?? 'unknown'}`}
      >
        {renderTypeIcon(type)}
      </div>

      {/* task-card */}
      <div
        className="flex-1 min-w-0 rounded-lg"
        style={{
          padding: '10px 12px',
          background: isRunning
            ? 'linear-gradient(180deg, var(--tp-bg-card) 0%, rgba(74, 222, 128, 0.05) 100%)'
            : 'var(--tp-bg-card)',
          border: `1px solid ${isRunning ? 'rgba(74, 222, 128, 0.4)' : 'var(--tp-border)'}`,
          boxShadow: isRunning
            ? '0 0 0 1px rgba(74, 222, 128, 0.2), 0 6px 16px rgba(74, 222, 128, 0.06)'
            : undefined,
        }}
      >
        {/* task-head */}
        <div className="flex items-center gap-1.5 mb-1.5">
          <div className="flex items-center gap-1.5 flex-1 min-w-0">
            {type && (
              <span
                className="text-[9.5px] font-bold tracking-wider font-mono px-1.5 rounded shrink-0"
                style={{
                  height: 18,
                  display: 'inline-flex',
                  alignItems: 'center',
                  background: `var(--type-${type}-bg-chip)`,
                  color: `var(--type-${type})`,
                }}
              >
                {TYPE_LABEL[type]}
              </span>
            )}
            <span
              className="text-[12.5px] font-semibold font-mono truncate"
              style={{ color: 'var(--tp-text)' }}
              title={task.workflow_name || `#${task.id}`}
            >
              {task.workflow_name || `#${task.id}`}
            </span>
          </div>
          {elapsedMs != null && (
            <span
              className="text-[10px] font-mono tabular-nums shrink-0"
              style={{ color: 'var(--tp-text-muted)' }}
              title="已运行时长"
            >
              {formatElapsed(elapsedMs)}
            </span>
          )}
          <span
            className="text-[10px] font-semibold tracking-wider uppercase inline-flex items-center gap-1 shrink-0 rounded"
            style={{
              height: 18,
              padding: '0 7px',
              background: isRunning ? 'rgba(74, 222, 128, 0.13)' : 'var(--tp-bg-elevated)',
              color: isRunning ? 'var(--status-running)' : 'var(--tp-text-muted)',
            }}
          >
            {isRunning && (
              <span
                style={{
                  width: 5, height: 5, borderRadius: '50%',
                  background: 'var(--status-running)',
                  boxShadow: '0 0 6px var(--status-running)',
                }}
              />
            )}
            {task.status === 'queued' ? 'queued' : 'running'}
          </span>
        </div>

        {/* task-meta */}
        <div
          className="flex items-center gap-1.5 text-[11px] font-mono"
          style={{ color: 'var(--tp-text-muted)' }}
        >
          {getMetaLine(task).split(' · ').map((seg, i, arr) => (
            <span key={i} className="flex items-center gap-1.5">
              <span>{seg}</span>
              {i < arr.length - 1 && <span style={{ color: 'var(--tp-text-ghost)' }}>·</span>}
            </span>
          ))}
        </div>

        {/* task-callout(L3 progress)*/}
        {isRunning && comfyNode && (
          <div className="mt-2 flex flex-wrap items-center gap-x-1.5 text-[11px]" style={{ color: 'var(--status-running)' }}>
            <Zap size={12} aria-hidden="true" />
            <span className="min-w-0 break-words">{comfyNode.label}</span>
            {comfyNode.steps && <span className="font-mono tabular-nums">{comfyNode.steps.step}/{comfyNode.steps.total} 步</span>}
          </div>
        )}
        {!comfyNode && showL3 && (
          <div
            className="mt-2 flex items-center gap-1.5 text-[11px] font-mono rounded"
            style={{
              padding: '7px 10px',
              background: 'rgba(74, 222, 128, 0.08)',
              borderLeft: '2px solid var(--status-running)',
              color: 'var(--status-running)',
            }}
          >
            <Zap size={12} strokeWidth={2} />
            <span>
              <span
                className="font-semibold"
                style={{ color: 'var(--status-running-strong, #86efac)' }}
              >
                {STAGE_LABEL[stage!] ?? stage}
                {step ? ` · step ${step.done}/${step.total}` : ' …'}
              </span>
              {step && stepLatencyMs != null && stepLatencyMs > 0 && (
                <> · {formatMs(stepLatencyMs)}/step</>
              )}
              {step && etaMs != null && etaMs > 0 && (
                <> · ETA {formatMs(etaMs)}</>
              )}
            </span>
          </div>
        )}

        {/* 整体进度条(ComfyUI「总进度」):running 全程一条总进度。determinate 按 % 填充 +
            右侧显数字;indeterminate(加载中/未出 stage)走滑动动画不造假 %。 */}
        {isRunning && (
          <div className="mt-2 flex items-center gap-2">
            <div
              role="progressbar"
              aria-label={comfyNode ? '当前节点进度' : '任务进度'}
              aria-valuenow={displayedPct ?? undefined}
              className="flex-1 overflow-hidden rounded-full"
              style={{ height: 4, background: 'var(--tp-border)' }}
            >
              {displayedPct != null ? (
                <div
                  style={{
                    width: `${displayedPct}%`,
                    height: '100%',
                    background: 'var(--status-running)',
                    borderRadius: 999,
                    transition: 'width 0.3s ease',
                  }}
                />
              ) : (
                <div
                  style={{
                    width: '30%',
                    height: '100%',
                    background: 'var(--status-running)',
                    borderRadius: 999,
                    animation: 'nous-indeterminate 1.1s ease-in-out infinite',
                  }}
                />
              )}
            </div>
            <span
              className="text-[10px] font-mono tabular-nums shrink-0"
              style={{ color: 'var(--tp-text-muted)', minWidth: 30, textAlign: 'right' }}
            >
              {displayedPct != null ? `${displayedPct}%` : '···'}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
