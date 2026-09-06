import { describe, it, expect } from 'vitest'
import { sortProcesses, formatRate, type ProcSortKey } from './processSort'
import type { ProcessInfo } from '../../api/system'

function row(pid: number, cpu: number, mem: number, tx: number | null, rx: number | null): ProcessInfo {
  return { pid, name: `p${pid}`, cpu_percent: cpu, memory_mb: mem, command: '', net_tx_bps: tx, net_rx_bps: rx }
}

const rows = [row(1, 5, 300, 10, 0), row(2, 50, 100, null, null), row(3, 20, 200, 500, 700)]

describe('sortProcesses', () => {
  it('cpu desc 是默认视角', () => {
    expect(sortProcesses(rows, 'cpu', 'desc').map((r) => r.pid)).toEqual([2, 3, 1])
  })
  it('mem asc', () => {
    expect(sortProcesses(rows, 'mem', 'asc').map((r) => r.pid)).toEqual([2, 3, 1])
  })
  it('net 按 tx+rx,null 当 0,desc', () => {
    expect(sortProcesses(rows, 'net', 'desc').map((r) => r.pid)).toEqual([3, 1, 2])
  })
  it('不改原数组', () => {
    const copy = [...rows]
    sortProcesses(rows, 'net' as ProcSortKey, 'desc')
    expect(rows).toEqual(copy)
  })
})

describe('formatRate', () => {
  it('null → —', () => expect(formatRate(null)).toBe('—'))
  it('< 1KB 用 B/s', () => expect(formatRate(512)).toBe('512 B/s'))
  it('< 1MB 用 KB/s 一位小数', () => expect(formatRate(12_600)).toBe('12.3 KB/s'))
  it('≥ 1MB 用 MB/s 一位小数', () => expect(formatRate(5 * 1024 * 1024)).toBe('5.0 MB/s'))
})
