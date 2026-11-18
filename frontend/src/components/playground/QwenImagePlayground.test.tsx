import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import type { ExposedParam } from '../../api/services'
import QwenImagePlayground from './QwenImagePlayground'

const fields: ExposedParam[] = [
  { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'text',
    constraints: { widget: 'radio', enum: ['text', 'remove'] } },
  { node_id: 'bridge', key: 'seed', type: 'integer', label: '种子',
    constraints: { min: 0, max: 100 } },
  { node_id: 'bridge', key: 'image', type: 'image', required: true,
    constraints: { visible_when: { input_mode: 'remove' } } },
]

it('运行时用已选种子，按递增或递减策略更新下一次的值', () => {
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={fields} initialInputs={{ seed: 42 }} running={false} onRun={onRun} />)
  fireEvent.click(screen.getByText('高级参数'))
  const mode = screen.getByLabelText('种子模式')
  const run = screen.getByRole('button', { name: '生成图片' })
  fireEvent.change(mode, { target: { value: 'increment' } })
  fireEvent.click(run)
  fireEvent.click(run)
  fireEvent.change(mode, { target: { value: 'decrement' } })
  fireEvent.click(run)
  expect(onRun.mock.calls.map(([input]) => input)).toEqual([
    { input_mode: 'text', seed: 42 },
    { input_mode: 'text', seed: 43 },
    { input_mode: 'text', seed: 44 },
  ])
  expect(screen.getByRole('spinbutton', { name: '种子' })).toHaveValue(43)
})

it('立即随机生成种子，运行时提交屏幕上显示的值', () => {
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={fields} running={false} onRun={onRun} />)
  fireEvent.click(screen.getByText('高级参数'))
  fireEvent.click(screen.getByRole('button', { name: '随机种子' }))
  const seed = (screen.getByRole('spinbutton', { name: '种子' }) as HTMLInputElement).valueAsNumber
  expect(seed).toBeGreaterThanOrEqual(0)
  expect(seed).toBeLessThanOrEqual(100)
  fireEvent.click(screen.getByRole('button', { name: '生成图片' }))
  expect(onRun).toHaveBeenCalledWith({ input_mode: 'text', seed })
})
