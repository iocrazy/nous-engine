import type { ComfyProgress } from '../api/predictions'

export function comfyNodeProgress({ phase, label, step, total }: ComfyProgress) {
  const nodeLabel = phase === 'running' ? label ?? '生成中'
    : phase === 'queued' ? '等待 ComfyUI' : '准备任务'
  if (phase !== 'running' || step == null || total == null || total <= 0) return { label: nodeLabel }
  return { label: nodeLabel, steps: { step, total, percent: Math.round(step / total * 100) } }
}
