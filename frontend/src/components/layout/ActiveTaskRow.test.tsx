import { act, render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { ExecutionTask } from '../../api/tasks'
import { useTaskProgressStore } from '../../stores/taskProgress'
import ActiveTaskRow from './ActiveTaskRow'

it('shows ComfyUI node progress from the initial snapshot and subsequent task events', () => {
  const task: ExecutionTask = {
    id: 'test-comfy-task', workflow_id: null, workflow_name: 'nous-qwen21-all-in-one',
    status: 'running', nodes_total: 1, nodes_done: 0, current_node: null,
    result: null, error: null, duration_ms: null,
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    task_type: null, image_width: null, image_height: null,
    comfy_progress: { phase: 'running', label: 'Qwen 多图编辑提示词' },
  }
  render(<ActiveTaskRow task={task} />)
  const bar = screen.getByRole('progressbar', { name: '当前节点进度' })
  expect(screen.getByText('Qwen 多图编辑提示词')).toBeVisible()
  expect(bar).not.toHaveAttribute('aria-valuenow')

  const progressStore = useTaskProgressStore.getState()
  act(() => progressStore.setProgress(task.id, { comfy: { phase: 'running', label: 'Qwen 采样', step: 3, total: 8 } }))
  expect(screen.getByText('Qwen 采样')).toBeVisible()
  expect(screen.getByText('3/8 步')).toBeVisible()
  expect(screen.getByText('38%')).toBeVisible()
  expect(bar).toHaveAttribute('aria-valuenow', '38')

  act(() => progressStore.setProgress(task.id, { comfy: { phase: 'running', label: 'VAE 解码' } }))
  expect(screen.getByText('VAE 解码')).toBeVisible()
  expect(screen.queryByText('38%')).not.toBeInTheDocument()
  expect(bar).not.toHaveAttribute('aria-valuenow')
  act(() => progressStore.clear(task.id))
})
