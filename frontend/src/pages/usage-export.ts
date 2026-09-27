import type { Timeseries, TopKeys, UsageSummary } from '../api/usage'

export interface ExportUsageArgs {
  days: number
  summary: UsageSummary | undefined
  series: Timeseries | undefined
  topKeys: TopKeys | undefined
}

/**
 * Build a CSV string out of the three usage queries and trigger a
 * browser download. Lives in its own module so the m02-style page file
 * exports only its component (Vite's react-refresh rule).
 */
export function exportUsageCsv({ days, summary, series, topKeys }: ExportUsageArgs) {
  const lines: string[] = []
  lines.push(`# nous-engine usage export · last ${days} days · ${new Date().toISOString()}`)
  lines.push('')

  if (summary) {
    lines.push('# summary')
    lines.push('metric,value')
    lines.push(`total_calls,${summary.total_calls}`)
    lines.push(`total_tokens,${summary.total_tokens}`)
    lines.push(`prompt_tokens,${summary.prompt_tokens}`)
    lines.push(`completion_tokens,${summary.completion_tokens}`)
    lines.push(`tts_characters,${summary.tts_characters}`)
    lines.push(`avg_latency_ms,${summary.avg_latency_ms ?? ''}`)
    lines.push(`prev_total_calls,${summary.prev_total_calls}`)
    lines.push(`prev_total_tokens,${summary.prev_total_tokens}`)
    lines.push('')
  }

  if (series && series.points.length > 0) {
    lines.push('# timeseries (calls per day per service)')
    const serviceCols = Array.from(
      new Set(series.points.flatMap((p) => Object.keys(p.by_service))),
    )
    lines.push(['date', ...serviceCols].join(','))
    for (const p of series.points) {
      lines.push([p.date, ...serviceCols.map((s) => p.by_service[s] ?? 0)].join(','))
    }
    lines.push('')
  }

  if (topKeys && topKeys.rows.length > 0) {
    lines.push('# top api keys')
    lines.push('label,key_prefix,mode,calls,tokens,avg_latency_ms')
    for (const r of topKeys.rows) {
      lines.push(
        [
          csvCell(r.label ?? ''),
          csvCell(r.key_prefix ?? ''),
          r.mode,
          r.calls,
          r.tokens,
          r.avg_latency_ms ?? '',
        ].join(','),
      )
    }
  }

  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `nous-usage-${days}d-${new Date().toISOString().slice(0, 10)}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function csvCell(s: string): string {
  if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`
  return s
}
