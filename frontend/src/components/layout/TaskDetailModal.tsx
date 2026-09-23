/**
 * TaskDetailModal — 任务面板重置 PR-3e:任务详情大图弹层。
 *
 * 触发:HistoryCard image expanded 点 thumb-64 / 「点击放大→」hint。
 * 渲染:body portal 820×600 居中浮层 + backdrop(0.6 不透明)。Esc 关闭 / 点 backdrop 关闭。
 *
 * 内部按 service type 切布局:
 *   image:480×480 大图(占左)+ 右侧参数 panel(prompt/seed/cfg/steps/duration)+ 重跑/复制/下载
 *   tts:大波形(占上)+ 文字 quote + 播放控制 + 时长
 *   llm:Q + A 完整文本 + token stats(prompt/completion/total)
 *   vision:输入图(占左)+ 输出描述(右)+ vision_completion_tokens
 *
 * spec 2026-05-27 task-panel-reset D7 形态。
 */
import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import {
  X, Image as ImageIcon, Mic, MessageSquare, Eye, Captions, Film,
  RefreshCw, Copy, Play,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useTasks, type ExecutionTask } from '../../api/tasks'
import { useServices } from '../../api/services'
import { useExecutionStore } from '../../stores/execution'
import { copyTextOrToast } from '../../utils/clipboard'

type TaskType = 'image' | 'tts' | 'vision' | 'llm' | 'asr' | 'video'

function getTaskType(t: ExecutionTask): TaskType | null {
  const v = (t as ExecutionTask & { type?: string }).type ?? t.task_type
  return (
    v === 'image' || v === 'tts' || v === 'vision' || v === 'llm' ||
    v === 'asr' || v === 'video'
  ) ? v : null
}

function getAudioDuration(t: ExecutionTask): number | null {
  return (t as ExecutionTask & { audio_duration_seconds?: number | null }).audio_duration_seconds ?? null
}

function getLlmTokens(t: ExecutionTask): { prompt: number | null; completion: number | null } {
  const o = t as ExecutionTask & {
    llm_prompt_tokens?: number | null
    llm_completion_tokens?: number | null
  }
  return { prompt: o.llm_prompt_tokens ?? null, completion: o.llm_completion_tokens ?? null }
}

function getVisionTokens(t: ExecutionTask): number | null {
  return (t as ExecutionTask & { vision_completion_tokens?: number | null })
    .vision_completion_tokens ?? null
}

/** PR-5:真实 task.result 形态 `{"outputs": {node_id: envelope}}` 兼容 iter。 */
function* iterNodeOutputs(t: ExecutionTask): Iterable<Record<string, unknown>> {
  if (!t.result || typeof t.result !== 'object') return
  const r = t.result as Record<string, unknown>
  const outputs = r.outputs as Record<string, unknown> | undefined
  const source = outputs && typeof outputs === 'object' ? outputs : r
  for (const v of Object.values(source)) {
    if (v && typeof v === 'object') yield v as Record<string, unknown>
  }
}

function findResultField<T>(t: ExecutionTask, key: string): T | null {
  for (const v of iterNodeOutputs(t)) {
    if (key in v && v[key] !== undefined) return v[key] as T
    const meta = v.meta as Record<string, unknown> | undefined
    if (meta && key in meta) return meta[key] as T
  }
  return null
}

function getThumbs(t: ExecutionTask): string[] {
  // 优先 output_thumbnails(V1.5 Lane I);否则从 result.outputs.{...}.image_url 收。
  const arr = (t as ExecutionTask & { output_thumbnails?: string[] | null }).output_thumbnails
  if (Array.isArray(arr) && arr.length > 0) return arr
  const urls: string[] = []
  for (const v of iterNodeOutputs(t)) {
    const u = v.image_url
    if (typeof u === 'string' && u) urls.push(u)
  }
  return urls
}

function durationLabel(ms: number | null): string {
  if (ms == null) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

export default function TaskDetailModal() {
  const taskId = useExecutionStore((s) => s.detailModalTaskId)
  const close = useExecutionStore((s) => s.closeDetailModal)
  const { data: tasks } = useTasks()
  const task = tasks?.find((t) => t.id === taskId) ?? null

  // Esc 关闭
  useEffect(() => {
    if (!task) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [task, close])

  if (!task) return null
  const type = getTaskType(task)

  const content = (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      onClick={close}
      style={{ background: 'rgba(0, 0, 0, 0.6)' }}
      role="dialog"
      aria-modal="true"
      aria-label="任务详情"
    >
      <div
        className="rounded-xl overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 820,
          height: 600,
          background: 'var(--tp-bg-panel)',
          border: '1px solid var(--tp-border-strong)',
          boxShadow: '0 24px 60px rgba(0, 0, 0, 0.75)',
        }}
      >
        <ModalHeader task={task} type={type} onClose={close} />
        <div className="flex-1 overflow-hidden">
          {type === 'image' && <ImageDetailBody task={task} />}
          {type === 'tts' && <TtsDetailBody task={task} />}
          {type === 'llm' && <LlmDetailBody task={task} />}
          {type === 'vision' && <VisionDetailBody task={task} />}
          {/* Arc 3:ASR 不做专属大改,按 result 通用字段降级展示(spec §8)。 */}
          {(type === 'asr' || !type) && <GenericDetailBody task={task} />}
        </div>
      </div>
    </div>
  )

  return createPortal(content, document.body)
}

/* ============================================================
 * Header
 * ============================================================ */
function ModalHeader({
  task, type, onClose,
}: {
  task: ExecutionTask
  type: TaskType | null
  onClose: () => void
}) {
  const TypeIcon = type === 'image' ? ImageIcon
                  : type === 'tts' ? Mic
                  : type === 'llm' ? MessageSquare
                  : type === 'vision' ? Eye
                  : type === 'asr' ? Captions
                  : type === 'video' ? Film
                  : null
  return (
    <div
      className="shrink-0 flex items-center gap-2.5 px-4"
      style={{ height: 52, borderBottom: '1px solid var(--tp-border-faint)' }}
    >
      {type && TypeIcon && (
        <div
          className="shrink-0 flex items-center justify-center rounded"
          style={{
            width: 28, height: 28,
            background: `var(--type-${type}-bg-chip)`,
            color: `var(--type-${type})`,
          }}
        >
          <TypeIcon size={14} />
        </div>
      )}
      <div className="flex-1 min-w-0 flex flex-col gap-0.5">
        <div
          className="text-sm font-semibold font-mono truncate"
          style={{ color: 'var(--tp-text)' }}
          title={task.workflow_name || `#${task.id}`}
        >
          {task.workflow_name || `#${task.id}`}
        </div>
        <div
          className="text-[11px] font-mono"
          style={{ color: 'var(--tp-text-muted)' }}
        >
          #{task.id} · {task.status} · {durationLabel(task.duration_ms)}
        </div>
      </div>
      <button
        onClick={onClose}
        aria-label="关闭"
        className="p-1.5 rounded transition-colors hover:bg-[var(--tp-bg-hover)]"
        style={{ color: 'var(--tp-text-muted)', cursor: 'pointer' }}
      >
        <X size={18} />
      </button>
    </div>
  )
}

/* ============================================================
 * Image body
 * ============================================================ */
function ImageDetailBody({ task }: { task: ExecutionTask }) {
  const thumbs = getThumbs(task)
  const cover = thumbs[0] ?? null
  const prompt = findResultField<string>(task, 'prompt')
  const seed = findResultField<number>(task, 'seed')
  const cfg = findResultField<number>(task, 'cfg_scale')
  const steps = findResultField<number>(task, 'steps')
  return (
    <div className="h-full flex">
      {/* 左:480×480 image area */}
      <div
        className="shrink-0 flex items-center justify-center"
        style={{
          width: 480, height: '100%',
          background: cover ? `center / contain no-repeat url(${JSON.stringify(cover)})` : 'var(--tp-bg-base)',
        }}
      >
        {!cover && (
          <div className="text-center" style={{ color: 'var(--tp-text-faint)' }}>
            <ImageIcon size={48} className="mx-auto mb-2" style={{ color: 'var(--type-image)' }} />
            <div className="text-xs">无缩略图</div>
          </div>
        )}
      </div>
      {/* 右:参数 panel + 操作 */}
      <div className="flex-1 min-w-0 flex flex-col" style={{ borderLeft: '1px solid var(--tp-border-faint)' }}>
        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-2.5">
          {prompt && (
            <Field label="prompt">
              <div
                className="text-[12px] font-mono leading-relaxed"
                style={{ color: 'var(--tp-text)' }}
              >
                {prompt}
              </div>
            </Field>
          )}
          <Field label="size">
            <Mono>{task.image_width}×{task.image_height}</Mono>
          </Field>
          {seed != null && <Field label="seed"><Mono>{seed}</Mono></Field>}
          {cfg != null && <Field label="cfg"><Mono>{cfg}</Mono></Field>}
          {steps != null && <Field label="steps"><Mono>{steps}</Mono></Field>}
          <Field label="duration"><Mono>{durationLabel(task.duration_ms)}</Mono></Field>
        </div>
        <ActionRow task={task} />
      </div>
    </div>
  )
}

/* ============================================================
 * TTS body
 * ============================================================ */
function TtsDetailBody({ task }: { task: ExecutionTask }) {
  const dur = getAudioDuration(task)
  const text = findResultField<string>(task, 'prompt') ?? findResultField<string>(task, 'text')
  return (
    <div className="h-full flex flex-col">
      {/* 大波形 */}
      <div
        className="shrink-0 mx-4 my-4 rounded-lg p-4 flex items-center gap-4"
        style={{
          background: 'var(--type-tts-audio-bg, #0e1a1d)',
          border: '1px solid var(--type-tts-audio-border, #1a3038)',
        }}
      >
        <button
          aria-label="播放"
          className="shrink-0 flex items-center justify-center rounded-full"
          style={{
            width: 44, height: 44,
            background: 'var(--type-tts)',
            color: '#0a0a0c',
            cursor: 'pointer',
          }}
        >
          <Play size={18} strokeWidth={2} fill="currentColor" />
        </button>
        <div className="flex-1 flex items-end gap-px h-12">
          {Array.from({ length: 80 }, (_, i) => {
            const h = 6 + ((i * 41 + 17) % 32)
            const played = i < 24
            return (
              <span
                key={i}
                style={{
                  width: 3,
                  height: h,
                  background: played ? 'var(--type-tts)' : 'var(--tp-text-faint)',
                  opacity: played ? 1 : 0.5,
                }}
              />
            )
          })}
        </div>
        <span
          className="shrink-0 text-[11px] font-mono"
          style={{ color: 'var(--tp-text-muted)' }}
        >
          {dur != null ? `0:00 / ${dur.toFixed(1)}s` : '—'}
        </span>
      </div>
      {/* 文字 quote */}
      <div className="flex-1 overflow-y-auto px-4 pb-4">
        <Field label="text">
          <div
            className="text-[13px] leading-relaxed p-3 rounded font-mono"
            style={{
              background: 'var(--type-tts-quote-bg, #0b1518)',
              border: '1px solid var(--type-tts-border-subtle, rgba(34, 211, 238, 0.25))',
              color: 'var(--tp-text)',
            }}
          >
            <span
              className="text-xl mr-1 font-serif"
              style={{ color: 'var(--type-tts)' }}
            >&ldquo;</span>
            {text || <span style={{ color: 'var(--tp-text-faint)' }}>(no text)</span>}
            <span
              className="text-xl ml-1 font-serif"
              style={{ color: 'var(--type-tts)' }}
            >&rdquo;</span>
          </div>
        </Field>
      </div>
      <ActionRow task={task} />
    </div>
  )
}

/* ============================================================
 * LLM body
 * ============================================================ */
function LlmDetailBody({ task }: { task: ExecutionTask }) {
  const { prompt, completion } = getLlmTokens(task)
  const text = findResultField<string>(task, 'text')
  const promptText = findResultField<string>(task, 'prompt')
  const total = (prompt ?? 0) + (completion ?? 0)
  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3">
        {promptText && (
          <Field label="prompt">
            <div
              className="text-[12px] font-mono leading-relaxed p-2.5 rounded"
              style={{
                background: 'var(--tp-bg-card)',
                border: '1px solid var(--tp-border)',
                color: 'var(--tp-text)',
              }}
            >
              {promptText}
            </div>
          </Field>
        )}
        <Field label="response">
          <div
            className="text-[12px] font-mono leading-relaxed p-3 rounded"
            style={{
              background: 'var(--type-llm-bg-history, rgba(96, 165, 250, 0.08))',
              border: '1px solid var(--type-llm-border-history, rgba(96, 165, 250, 0.35))',
              borderLeft: '3px solid var(--type-llm)',
              color: 'var(--tp-text)',
              maxHeight: 320,
              overflowY: 'auto',
            }}
          >
            {text || <span style={{ color: 'var(--tp-text-faint)' }}>(no text)</span>}
          </div>
        </Field>
        <div className="flex gap-3">
          {prompt != null && <Stat label="prompt" value={`${prompt}`} unit="tok" />}
          {completion != null && <Stat label="completion" value={`${completion}`} unit="tok" />}
          {total > 0 && <Stat label="total" value={`${total}`} unit="tok" />}
          <Stat label="duration" value={durationLabel(task.duration_ms)} />
        </div>
      </div>
      <ActionRow task={task} />
    </div>
  )
}

/* ============================================================
 * Vision body
 * ============================================================ */
function VisionDetailBody({ task }: { task: ExecutionTask }) {
  const ct = getVisionTokens(task)
  const text = findResultField<string>(task, 'text')
  const thumbs = getThumbs(task)
  const inputImg = thumbs[0] ?? null
  return (
    <div className="h-full flex">
      <div
        className="shrink-0 flex items-center justify-center"
        style={{
          width: 360, height: '100%',
          background: inputImg
            ? `center / contain no-repeat url(${JSON.stringify(inputImg)})`
            : 'var(--type-vision-bg-card, #1a0f06)',
        }}
      >
        {!inputImg && (
          <div className="text-center" style={{ color: 'var(--type-vision)' }}>
            <Eye size={48} className="mx-auto mb-2" />
            <div className="text-xs">输入图占位</div>
          </div>
        )}
      </div>
      <div className="flex-1 min-w-0 flex flex-col" style={{ borderLeft: '1px solid var(--tp-border-faint)' }}>
        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3">
          <Field label="description">
            <div
              className="text-[12px] font-mono leading-relaxed p-3 rounded"
              style={{
                background: 'var(--type-vision-bg-card, #1a0f06)',
                border: '1px solid var(--type-vision-border-subtle, rgba(251, 146, 60, 0.25))',
                color: 'var(--tp-text)',
              }}
            >
              {text || <span style={{ color: 'var(--tp-text-faint)' }}>(no description)</span>}
            </div>
          </Field>
          <div className="flex gap-3">
            {ct != null && <Stat label="output" value={`${ct}`} unit="tok" />}
            <Stat label="duration" value={durationLabel(task.duration_ms)} />
          </div>
        </div>
        <ActionRow task={task} />
      </div>
    </div>
  )
}

function GenericDetailBody({ task }: { task: ExecutionTask }) {
  return (
    <div className="p-4 text-xs font-mono" style={{ color: 'var(--tp-text-muted)' }}>
      <pre className="whitespace-pre-wrap break-words">
        {JSON.stringify(task.result, null, 2) || '(no result)'}
      </pre>
    </div>
  )
}

/* ============================================================
 * Subs
 * ============================================================ */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <div
        className="text-[10px] uppercase tracking-wider font-semibold font-mono"
        style={{ color: 'var(--tp-text-muted)' }}
      >
        {label}
      </div>
      <div>{children}</div>
    </div>
  )
}

function Mono({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[12px] font-mono" style={{ color: 'var(--tp-text)' }}>
      {children}
    </div>
  )
}

function Stat({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <div
        className="text-[10px] uppercase tracking-wider font-semibold font-mono"
        style={{ color: 'var(--tp-text-muted)' }}
      >
        {label}
      </div>
      <div className="text-[14px] font-mono font-semibold" style={{ color: 'var(--tp-text)' }}>
        {value}{unit && <span className="ml-1 text-[11px]" style={{ color: 'var(--tp-text-muted)' }}>{unit}</span>}
      </div>
    </div>
  )
}

function ActionRow({ task }: { task: ExecutionTask }) {
  // 重跑(相同参数):按 task.workflow_id 找到对应服务 → 跳服务 Playground,用 input_json
  // 回填表单(spec 2026-06-09 run-history PR-A)。复制参数:input_json → 剪贴板 JSON。
  const navigate = useNavigate()
  const close = useExecutionStore((s) => s.closeDetailModal)
  const { data: services } = useServices()
  const svc = services?.find((s) => !!s.workflow_id && s.workflow_id === task.workflow_id)
  const hasParams = !!task.input_json && Object.keys(task.input_json).length > 0
  const canRerun = !!svc && hasParams

  const rerun = () => {
    if (!svc) return
    close()
    navigate(`/services/${svc.id}`, { state: { rerunInputs: task.input_json } })
  }
  const copyParams = async () => {
    await copyTextOrToast(JSON.stringify(task.input_json ?? {}, null, 2), '已复制参数')
  }

  return (
    <div
      className="shrink-0 flex items-center gap-2 px-4 py-3"
      style={{ borderTop: '1px solid var(--tp-border-faint)' }}
    >
      <ActionBtn
        label={canRerun ? '重跑(相同参数)' : '重跑(需源服务+入参)'}
        primary
        disabled={!canRerun}
        onClick={rerun}
      >
        <RefreshCw size={14} />
      </ActionBtn>
      <ActionBtn label="复制参数" disabled={!hasParams} onClick={copyParams}>
        <Copy size={14} />
      </ActionBtn>
    </div>
  )
}

function ActionBtn({
  label, primary, children, onClick, disabled,
}: {
  label: string
  primary?: boolean
  children: React.ReactNode
  onClick?: () => void
  disabled?: boolean
}) {
  return (
    <button
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded text-[11.5px] font-mono transition-colors"
      style={{
        background: primary ? 'var(--type-image-bg-chip)' : 'var(--tp-bg-elevated)',
        border: `1px solid ${primary ? 'var(--type-image-border-subtle, var(--type-image))' : 'var(--tp-border-strong)'}`,
        color: primary ? 'var(--type-image)' : 'var(--tp-text-muted)',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {children}
      {label}
    </button>
  )
}
