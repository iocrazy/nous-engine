/**
 * useTaskProgressStore — 任务面板重置 PR-6:全局 L3 progress map(task_id → 进度状态)。
 *
 * useTasks 通过 WS `/ws/tasks` 监听 task list 增删改;PR-6 后 backend 在同一通道**额外**
 * 广播 `{event: "progress", task_id, stage, step, total_steps, step_latency_ms, eta_ms, ...}`。
 *
 * 多任务并发场景:每个 ActiveTaskRow 用 `useTaskProgressStore(s => s.byTaskId.get(task.id))`
 * 拿到该任务的最新 L3 数据,不依赖 useExecutionStore.taskId(那个只跟用户 Run 触发的单
 * task 挂钩)。任务终态时 store 清掉对应 entry,避免 ghost progress。
 */
import { create } from 'zustand'
import type { ComfyProgress } from '../api/predictions'

export interface TaskProgressState {
  comfy?: ComfyProgress
  stage: string | null
  step: number | null
  totalSteps: number | null
  stepLatencyMs: number | null
  etaMs: number | null
  progress: number | null
  /** 最近一次 update 的时间戳,UI 可用来 fade 旧数据 */
  updatedAt: number
}

interface Store {
  byTaskId: Map<string, TaskProgressState>
  setProgress: (taskId: string, partial: Partial<TaskProgressState>) => void
  clear: (taskId: string) => void
}

export const useTaskProgressStore = create<Store>((set) => ({
  byTaskId: new Map(),
  setProgress: (taskId, partial) =>
    set((s) => {
      const next = new Map(s.byTaskId)
      const prev = next.get(taskId) ?? {
        stage: null, step: null, totalSteps: null,
        stepLatencyMs: null, etaMs: null, progress: null, updatedAt: 0,
      }
      next.set(taskId, { ...prev, ...partial, updatedAt: Date.now() })
      return { byTaskId: next }
    }),
  clear: (taskId) =>
    set((s) => {
      if (!s.byTaskId.has(taskId)) return s
      const next = new Map(s.byTaskId)
      next.delete(taskId)
      return { byTaskId: next }
    }),
}))
