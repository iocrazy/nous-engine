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

it('OLDX 提示姿势与场景参考，人物资产卡沿用固定版式', () => {
  const assets: ExposedParam[] = [
    { node_id: 'bridge', key: 'input_mode', type: 'string', default: 'oldx',
      constraints: { widget: 'radio', enum: ['oldx', 'character_card'], option_meta: [
        { value: 'oldx', label: 'OLDX 人物替换' },
        { value: 'character_card', label: '人物资产卡' },
      ] } },
    { node_id: 'bridge', key: 'image', type: 'image', required: true },
    { node_id: 'bridge', key: 'oldx_target', type: 'image', label: '姿势与场景参考',
      constraints: { visible_when: { input_mode: 'oldx' }, required_when: { input_mode: 'oldx' } } },
  ]
  const onRun = vi.fn()
  render(<QwenImagePlayground inputs={assets}
    initialInputs={{ input_mode: 'oldx', image: 'data:image/png;base64,AA==' }} running={false} onRun={onRun} />)
  expect(screen.getByRole('button', { name: /^人物源图/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /姿势与场景参考/ })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '替换人物' }))
  expect(onRun).not.toHaveBeenCalled()
  expect(screen.getByRole('status')).toHaveTextContent('姿势与场景参考')
  fireEvent.click(screen.getByRole('radio', { name: '人物资产卡' }))
  expect(screen.queryByRole('button', { name: /姿势与场景参考/ })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '生成人物资产卡' }))
  expect(onRun).toHaveBeenCalledWith({ input_mode: 'character_card', image: 'data:image/png;base64,AA==' })
})
