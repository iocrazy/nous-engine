import { apiFetch } from './client'

// Cog 形状态(后端 src/services/prediction_service.py `_STATUS_MAP`):
// queued→starting, running→processing, completed→succeeded, failed→failed, cancelled→canceled。
export type PredictionStatus = 'starting' | 'processing' | 'succeeded' | 'failed' | 'canceled'

export interface ComfyProgress {
  phase: 'submitting' | 'queued' | 'running'
  label?: string
  step?: number
  total?: number
}

export interface Prediction {
  id: string
  service: string | null
  status: PredictionStatus
  input: Record<string, unknown>
  /** 仅 succeeded 时非 null(后端 task_to_prediction 只在终态成功时给 output)。 */
  output: Record<string, unknown> | null
  error: string | null
  progress: {
    nodes_done: number
    nodes_total: number
    comfy?: ComfyProgress
  }
  metrics: { predict_time?: number }
  created_at: string | null
  started_at: string | null
  completed_at: string | null
}

/**
 * 提交一个异步 prediction(`Prefer: respond-async` → 202,立即拿到 `starting` 态的
 * prediction,不阻塞到终态)。鉴权走 `apiFetch`(same-origin cookie)—— 管理后台
 * Playground 场景下后端 `predictions.py::_auth_predictions` 认 admin session 旁路
 * (镜像既有 `/v1/apps/{name}/run` 的 `_auth_apps_run`),无需 Bearer key。
 */
export function createPredictionAsync(
  serviceName: string,
  input: Record<string, unknown>,
): Promise<Prediction> {
  return apiFetch<Prediction>(`/v1/services/${encodeURIComponent(serviceName)}/predictions`, {
    method: 'POST',
    headers: { Prefer: 'respond-async' },
    body: JSON.stringify({ input }),
  })
}

export function getPrediction(id: string): Promise<Prediction> {
  return apiFetch<Prediction>(`/v1/predictions/${encodeURIComponent(id)}`)
}

export function cancelPrediction(id: string): Promise<Prediction> {
  return apiFetch<Prediction>(`/v1/predictions/${encodeURIComponent(id)}/cancel`, {
    method: 'POST',
  })
}
