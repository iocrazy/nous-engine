import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import IconRail from './IconRail'
import { useExecutionStore } from '../../stores/execution'

vi.mock('../../api/tasks', () => ({ useTasks: () => ({ data: [] }) }))
vi.mock('../../api/admin', () => ({
  useAdminLogout: () => ({ mutate: vi.fn(), isPending: false }),
  useAdminMe: () => ({ data: { login_required: false } }),
}))
vi.mock('../../stores/theme', () => ({
  useThemeStore: (sel?: (s: { mode: string; setMode: ReturnType<typeof vi.fn> }) => unknown) => {
    const state = { mode: 'dark', setMode: vi.fn() }
    return sel ? sel(state) : state
  },
}))

describe('IconRail TaskRailButton — badge from taskIconBadge', () => {
  beforeEach(() => {
    useExecutionStore.setState({ taskIconBadge: 0, taskPanelOpen: false })
  })

  it('shows the taskIconBadge count (not running-task count)', () => {
    useExecutionStore.setState({ taskIconBadge: 3 })
    render(<MemoryRouter><IconRail /></MemoryRouter>)
    expect(screen.getByText('3')).toBeTruthy()
  })

  it('clicking the Tasks button clears the badge', () => {
    useExecutionStore.setState({ taskIconBadge: 2 })
    render(<MemoryRouter><IconRail /></MemoryRouter>)
    fireEvent.click(screen.getByLabelText('Tasks'))
    expect(useExecutionStore.getState().taskIconBadge).toBe(0)
  })

  it('hides the badge when count is 0', () => {
    render(<MemoryRouter><IconRail /></MemoryRouter>)
    expect(screen.queryByText('0')).toBeNull()
  })
})


it('keeps the dashboard URL and its search params when clicking the active sidebar entry', () => {
  function CurrentLocation() {
    const location = useLocation()
    return <output aria-label="当前地址">{location.pathname}{location.search}</output>
  }

  render(
    <MemoryRouter initialEntries={['/dashboard?tab=system']}>
      <IconRail />
      <CurrentLocation />
    </MemoryRouter>,
  )
  fireEvent.click(screen.getByRole('button', { name: '概览' }))
  expect(screen.getByLabelText('当前地址').textContent).toBe('/dashboard?tab=system')
})
