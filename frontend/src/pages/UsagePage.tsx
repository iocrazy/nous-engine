import { useMemo, useState } from 'react'
import { Download } from 'lucide-react'
import { exportUsageCsv } from './usage-export'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  useUsageSummary,
  useUsageTimeseries,
  useUsageTopKeys,
  type Timeseries,
} from '../api/usage'

const RANGES = [
  { id: 7, label: '最近 7 天' },
  { id: 30, label: '最近 30 天' },
  { id: 90, label: '最近 90 天' },
] as const

type DaysOpt = (typeof RANGES)[number]['id']

const SERIES_COLORS = [
  '#22c55e', // accent-2 / green
  '#6366f1', // accent / indigo
  '#f59e0b', // warn / amber
  '#a855f7', // purple
  '#3b82f6', // info / blue
  '#888888', // other / muted
]

export default function UsagePage() {
  const [days, setDays] = useState<DaysOpt>(7)
  const summary = useUsageSummary(days)
  const series = useUsageTimeseries(days)
  const topKeys = useUsageTopKeys(days, 10)

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        overflow: 'auto',
        background: 'var(--bg)',
      }}
    >
      <div className="console-page console-controls">
        <div className="console-page-header">
          <div style={{ flex: 1 }}>
            <h1 className="console-title">用量统计</h1>
            <p className="console-description">
              调用量 · token 消耗 · 按 Key 按服务按时段的聚合
            </p>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value) as DaysOpt)}
              style={{
                background: 'var(--bg-accent)',
                color: 'var(--text)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                padding: '7px 10px',
                fontSize: 12,
              }}
            >
              {RANGES.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() =>
                exportUsageCsv({
                  days,
                  summary: summary.data,
                  series: series.data,
                  topKeys: topKeys.data,
                })
              }
              disabled={!summary.data && !series.data && !topKeys.data}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 4,
                padding: '7px 12px',
                background: 'var(--bg-accent)',
                color: 'var(--text)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                fontSize: 12,
                cursor: 'pointer',
              }}
            >
              <Download size={12} />
              导出 CSV
            </button>
          </div>
        </div>

        {/* stat cards */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 150px), 1fr))',
            gap: 12,
            marginBottom: 14,
          }}
        >
          <Stat
            label="总调用"
            value={fmtCount(summary.data?.total_calls)}
            sub={pctChange(summary.data?.total_calls, summary.data?.prev_total_calls)}
          />
          <Stat
            label="总 token"
            value={fmtCount(summary.data?.total_tokens)}
            sub={`prompt ${fmtCount(summary.data?.prompt_tokens)} · completion ${fmtCount(summary.data?.completion_tokens)}`}
          />
          <Stat
            label="平均延迟"
            value={summary.data?.avg_latency_ms != null
              ? `${(summary.data.avg_latency_ms / 1000).toFixed(2)}s`
              : '—'}
            sub={summary.data?.tts_characters
              ? `TTS 字符：${fmtCount(summary.data.tts_characters)}`
              : ''}
          />
          <Stat
            label="错误率"
            value={summary.data?.error_rate != null ? `${(summary.data.error_rate * 100).toFixed(2)}%` : '—'}
            sub="（暂未采集）"
            muted
          />
        </div>

        {/* timeseries chart */}
        <Panel title="每日调用量（按服务）">
          <div style={{ height: 280, padding: '10px 4px' }}>
            {series.isLoading && <CenterMsg>加载中…</CenterMsg>}
            {series.error && <CenterMsg error>{(series.error as Error).message}</CenterMsg>}
            {series.data && <Chart data={series.data} />}
          </div>
        </Panel>

        {/* top keys table */}
        <Panel title="Top API Key（按调用量）">
          {topKeys.isLoading && <CenterMsg>加载中…</CenterMsg>}
          {topKeys.error && <CenterMsg error>{(topKeys.error as Error).message}</CenterMsg>}
          {topKeys.data && (
            <table className="console-table">
              <thead>
                <tr style={{ color: 'var(--muted)', fontSize: 11, textAlign: 'left' }}>
                  <th style={th}>Label</th>
                  <th style={th}>模式</th>
                  <th style={{ ...th, textAlign: 'right' }}>调用</th>
                  <th style={{ ...th, textAlign: 'right' }}>Token</th>
                  <th style={{ ...th, textAlign: 'right' }}>平均延迟</th>
                </tr>
              </thead>
              <tbody>
                {topKeys.data.rows.length === 0 && (
                  <tr>
                    <td colSpan={5} style={{ ...td, textAlign: 'center', color: 'var(--muted)' }}>
                      该窗口内暂无调用
                    </td>
                  </tr>
                )}
                {topKeys.data.rows.map((row) => (
                  <tr key={row.api_key_id} style={{ borderTop: '1px solid var(--border)' }}>
                    <td style={td}>
                      <strong style={{ color: 'var(--text)' }}>{row.label ?? '(unlabeled)'}</strong>
                      {row.key_prefix && (
                        <code
                          style={{
                            color: 'var(--muted)',
                            fontFamily: 'var(--mono, monospace)',
                            marginLeft: 6,
                            fontSize: 10,
                          }}
                        >
                          {row.key_prefix}…
                        </code>
                      )}
                    </td>
                    <td style={td}>
                      <ModeBadge mode={row.mode} />
                    </td>
                    <td style={{ ...td, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {fmtCount(row.calls)}
                    </td>
                    <td style={{ ...td, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {fmtCount(row.tokens)}
                    </td>
                    <td style={{ ...td, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                      {row.avg_latency_ms != null
                        ? `${(row.avg_latency_ms / 1000).toFixed(2)}s`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  )
}

// ---------- chart ----------

function Chart({ data }: { data: Timeseries }) {
  const seriesNames = useMemo(() => {
    const names = new Set<string>()
    for (const p of data.points) for (const k of Object.keys(p.by_service)) names.add(k)
    return Array.from(names)
  }, [data])

  const chartData = useMemo(
    () =>
      data.points.map((p) => ({
        date: shortDate(p.date),
        ...Object.fromEntries(seriesNames.map((s) => [s, p.by_service[s] ?? 0])),
      })),
    [data, seriesNames],
  )

  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={chartData} stackOffset="sign">
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis dataKey="date" stroke="var(--muted)" fontSize={11} />
        <YAxis stroke="var(--muted)" fontSize={11} />
        <Tooltip
          contentStyle={{
            background: 'var(--bg-elevated, var(--bg))',
            border: '1px solid var(--border)',
            borderRadius: 4,
            fontSize: 12,
          }}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        {seriesNames.map((s, i) => (
          <Bar key={s} dataKey={s} stackId="a" fill={SERIES_COLORS[i % SERIES_COLORS.length]} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  )
}

function shortDate(iso: string): string {
  // YYYY-MM-DD → MM/DD
  return iso.slice(5).replace('-', '/')
}


// ---------- bits ----------

function Stat({
  label,
  value,
  sub,
  muted,
}: {
  label: string
  value: string
  sub?: string
  muted?: boolean
}) {
  return (
    <div className="console-card"
      style={{
        padding: '14px 16px',
      }}
    >
      <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 24,
          fontWeight: 600,
          marginTop: 4,
          color: muted ? 'var(--muted)' : 'var(--text)',
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>{sub}</div>
      )}
    </div>
  )
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="console-card"
      style={{
        marginBottom: 12,
      }}
    >
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--border)',
          fontSize: 13,
          color: 'var(--text)',
          fontWeight: 500,
        }}
      >
        {title}
      </div>
      <div style={{ padding: 12 }}>{children}</div>
    </div>
  )
}

function CenterMsg({
  children,
  error,
}: {
  children: React.ReactNode
  error?: boolean
}) {
  return (
    <div
      style={{
        textAlign: 'center',
        padding: 32,
        color: error ? 'var(--error, #ef4444)' : 'var(--muted)',
        fontSize: 13,
      }}
    >
      {children}
    </div>
  )
}

function ModeBadge({ mode }: { mode: 'legacy' | 'm:n' }) {
  const style =
    mode === 'm:n'
      ? { background: 'rgba(34,197,94,0.15)', color: 'var(--accent-2, #22c55e)' }
      : { background: 'var(--bg)', color: 'var(--muted)', border: '1px solid var(--border)' }
  return (
    <span style={{ fontSize: 10, padding: '1px 7px', borderRadius: 10, ...style }}>
      {mode === 'm:n' ? 'M:N' : 'Legacy'}
    </span>
  )
}

const th = { padding: '8px 12px', fontWeight: 500 } as const
const td = { padding: '8px 12px', color: 'var(--text)' } as const

function fmtCount(n: number | undefined): string {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function pctChange(curr?: number, prev?: number): string {
  if (curr == null || prev == null || prev === 0) return ''
  const pct = ((curr - prev) / prev) * 100
  const arrow = pct >= 0 ? '↑' : '↓'
  return `${arrow} ${Math.abs(pct).toFixed(1)}% vs 上期`
}
