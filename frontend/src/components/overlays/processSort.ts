import type { ProcessInfo } from '../../api/system'

export type ProcSortKey = 'cpu' | 'mem' | 'net'
export type ProcSortDir = 'asc' | 'desc'

function keyOf(r: ProcessInfo, key: ProcSortKey): number {
  switch (key) {
    case 'cpu':
      return r.cpu_percent
    case 'mem':
      return r.memory_mb
    case 'net':
      return (r.net_tx_bps ?? 0) + (r.net_rx_bps ?? 0)
  }
}

/** 不改原数组;同值按 pid 稳定。 */
export function sortProcesses(rows: ProcessInfo[], key: ProcSortKey, dir: ProcSortDir): ProcessInfo[] {
  const sign = dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const d = keyOf(a, key) - keyOf(b, key)
    return d !== 0 ? sign * d : a.pid - b.pid
  })
}

/** 字节/秒 → 人看的速率;null(采集器不可用)→ '—'。 */
export function formatRate(bps: number | null): string {
  if (bps === null || bps === undefined) return '—'
  if (bps < 1024) return `${Math.round(bps)} B/s`
  if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`
  return `${(bps / 1024 / 1024).toFixed(1)} MB/s`
}
