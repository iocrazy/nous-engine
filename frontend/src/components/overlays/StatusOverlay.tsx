import { CheckCircle2, AlertTriangle, XCircle, HeartPulse, CircleDashed } from 'lucide-react'
import { useStatus, type ComponentStatus, type DayStatus, type StatusComponent } from '../../api/status'

/**
 * 状态页(对齐 status.claude.ai 风格,admin-gated)。
 * 顶部总体横幅 + 每组件一行(状态点 + 名称 + 7 天 uptime 条 + 7 天 uptime%)。
 * 数据来自 GET /api/v1/status,15s 自动刷新。
 * idle = 基础设施在线但没加载模型(灰,不是绿"运行正常"也不是红"中断")。
 */

const COLOR: Record<DayStatus, string> = {
  operational: '#22c55e', // green
  idle: '#9ca3af',        // gray — 在线但空闲
  degraded: '#f59e0b',    // amber
  down: '#ef4444',        // red
  nodata: 'var(--border, #3a3a3a)',
}

const LABEL: Record<ComponentStatus, string> = {
  operational: '运行正常',
  idle: '空闲(未加载)',
  degraded: '部分降级',
  down: '服务中断',
}

function statusIcon(s: ComponentStatus, size = 18) {
  if (s === 'operational') return <CheckCircle2 size={size} style={{ color: COLOR.operational }} />
  if (s === 'idle') return <CircleDashed size={size} style={{ color: COLOR.idle }} />
  if (s === 'degraded') return <AlertTriangle size={size} style={{ color: COLOR.degraded }} />
  return <XCircle size={size} style={{ color: COLOR.down }} />
}

function UptimeBar({ days }: { days: StatusComponent['days'] }) {
  return (
    <div style={{ display: 'flex', gap: 3, alignItems: 'flex-end' }}>
      {days.map((d) => (
        <div
          key={d.date}
          title={
            d.status === 'nodata'
              ? `${d.date} · 无数据`
              : `${d.date} · uptime ${d.uptime_pct}% (${d.samples} 采样)`
          }
          style={{
            width: 10,
            height: 30,
            borderRadius: 2,
            background: COLOR[d.status],
            opacity: d.status === 'nodata' ? 0.4 : 1,
          }}
        />
      ))}
    </div>
  )
}

export default function StatusOverlay() {
  const { data, isLoading, error } = useStatus()

  const overall = data?.overall ?? 'operational'
  const bannerColor = COLOR[overall === 'operational' ? 'operational' : overall === 'degraded' ? 'degraded' : 'down']

  return (
    <div className="absolute inset-0 overflow-y-auto z-[16]" style={{ background: 'var(--bg)' }}>
      <div style={{ maxWidth: 920, margin: '0 auto', padding: 20 }}>
        <div style={{ marginBottom: 14, display: 'flex', alignItems: 'center', gap: 8 }}>
          <HeartPulse size={20} style={{ color: 'var(--text)' }} />
          <div>
            <h1 style={{ fontSize: 20, color: 'var(--text)', fontWeight: 600 }}>系统状态</h1>
            <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 2 }}>
              各组件实时健康 + 过去 7 天可用率(15s 自动刷新)
            </p>
          </div>
        </div>

        {isLoading && <div style={{ color: 'var(--muted)', fontSize: 13 }}>加载中…</div>}
        {error && (
          <div style={{ color: COLOR.down, fontSize: 13 }}>
            状态加载失败:{(error as Error)?.message ?? String(error)}
          </div>
        )}

        {data && (
          <>
            {/* 总体横幅 */}
            <div
              style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '14px 16px', borderRadius: 10, marginBottom: 16,
                background: 'var(--surface, #1a1a1a)',
                borderLeft: `4px solid ${bannerColor}`,
              }}
            >
              {statusIcon(overall, 22)}
              <div style={{ fontSize: 16, fontWeight: 600, color: 'var(--text)' }}>
                {overall === 'operational' ? '所有系统运行正常' : overall === 'degraded' ? '部分系统降级' : '存在服务中断'}
              </div>
              <div style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--muted)' }}>
                更新于 {new Date(data.updated_at).toLocaleTimeString()}
              </div>
            </div>

            {/* 组件列表 */}
            <div
              style={{
                background: 'var(--surface, #1a1a1a)', borderRadius: 10,
                border: '1px solid var(--border, #2a2a2a)', overflow: 'hidden',
              }}
            >
              {data.components.map((c, i) => (
                <div
                  key={c.key}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 12, padding: '14px 16px',
                    borderTop: i === 0 ? 'none' : '1px solid var(--border, #2a2a2a)',
                  }}
                >
                  {statusIcon(c.status)}
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 14, color: 'var(--text)', fontWeight: 500 }}>{c.name}</div>
                    <div style={{ fontSize: 12, color: COLOR[c.status] }}>{LABEL[c.status]}</div>
                    {c.detail && (
                      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>{c.detail}</div>
                    )}
                  </div>
                  <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 14 }}>
                    <UptimeBar days={c.days} />
                    <div style={{ fontSize: 13, color: 'var(--muted)', minWidth: 70, textAlign: 'right' }}>
                      {c.uptime_7d == null ? '— ' : `${c.uptime_7d}%`}
                      <div style={{ fontSize: 10, color: 'var(--muted)' }}>7 天可用率</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
