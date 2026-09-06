import { useState, type CSSProperties } from 'react'
import { Activity, AlertTriangle, BarChart3, ChevronRight, Cpu, X } from 'lucide-react'
import {
  useSysGpus, useSysStats, useSysProcesses, useKillProcess,
  type SysGpuInfo, type GpuProcessInfo,
} from '../../api/system'
import {
  useEngines, useUnloadImageAdapters, useLoadedAdapters, useGpuGroups,
  type GpuGroup,
} from '../../api/engines'
import { useDashboardSummary, type AlertItem, type TopServiceRow } from '../../api/dashboard'
import { useRuntimeMetrics, type RuntimeSnapshot } from '../../api/observability'
import { useVLLMMetrics, useUpdateLaunchParams } from '../../api/vllm'
import { useRunners, type RunnerInfo } from '../../api/runners'
import { confirmDialog } from '../../stores/confirm'
import { sortProcesses, formatRate, type ProcSortKey, type ProcSortDir } from './processSort'

/**
 * m04 Dashboard — v3 layout.
 *
 * Top: 4 business stats (today's calls / month tokens / active alerts /
 *   key·service counts).
 * Mid: alerts feed + today's top services.
 * Bottom: collapsible "system status" with GPU panels, mini stats,
 *   loaded models, processes table — keeps the v2 monitoring detail
 *   accessible without dominating the page.
 */
export default function DashboardOverlay() {
  const summary = useDashboardSummary()
  const [sysOpen, setSysOpen] = useState(false)

  return (
    <div
      className="absolute inset-0 overflow-y-auto z-[16]"
      style={{ background: 'var(--bg)' }}
    >
      <div style={{ maxWidth: 1200, margin: '0 auto', padding: 20 }}>
        <div style={{ marginBottom: 14 }}>
          <h1 style={{ fontSize: 20, color: 'var(--text)', fontWeight: 600 }}>概览</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 4 }}>
            今日业务鸟瞰 + 系统状态
          </p>
        </div>

        {/* Top business stats */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(4, 1fr)',
            gap: 12,
            marginBottom: 12,
          }}
        >
          <BizStat
            label="今日调用"
            value={fmtN(summary.data?.today_calls)}
            sub={deltaText(summary.data?.today_calls_delta_pct)}
            subColor={
              summary.data?.today_calls_delta_pct == null
                ? 'var(--muted)'
                : summary.data.today_calls_delta_pct >= 0
                  ? 'var(--accent-2, #22c55e)'
                  : 'var(--warn, #f59e0b)'
            }
          />
          <BizStat
            label="本月 token 用量"
            value={fmtN(summary.data?.month_tokens)}
            sub={
              summary.data?.month_tokens_quota
                ? `占 ${fmtN(summary.data.month_tokens_quota)} 配额 ${summary.data.month_tokens_used_pct}%`
                : '暂未挂资源包'
            }
          />
          <BizStat
            label="活跃告警"
            value={summary.data ? String(summary.data.active_alerts_count) : '—'}
            valueColor={
              (summary.data?.active_alerts_count ?? 0) > 0
                ? 'var(--warn, #f59e0b)'
                : undefined
            }
            sub={summary.data?.active_alerts_top_label ?? '一切正常'}
          />
          <BizStat
            label="API Key · 实例"
            value={
              summary.data
                ? `${summary.data.api_key_count} · ${summary.data.service_count}`
                : '—'
            }
            sub={
              summary.data && summary.data.unbound_key_count > 0
                ? `${summary.data.unbound_key_count} key 未绑定`
                : ''
            }
          />
        </div>

        {/* Alerts feed */}
        <Panel
          icon={<AlertTriangle size={14} style={{ color: 'var(--warn, #f59e0b)' }} />}
          title="活跃告警 · 最近触发"
        >
          {(summary.data?.recent_alerts.length ?? 0) === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--muted)', padding: '8px 0' }}>
              过去 7 天无告警触发
            </div>
          ) : (
            summary.data!.recent_alerts.map((a) => <AlertRow key={a.id} alert={a} />)
          )}
        </Panel>

        {/* Top services today */}
        <Panel
          icon={<BarChart3 size={14} style={{ color: 'var(--accent-2, #22c55e)' }} />}
          title="今日 Top 调用服务"
        >
          {(summary.data?.top_services_today.length ?? 0) === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--muted)', padding: '8px 0' }}>
              今日暂无 LLM 调用
            </div>
          ) : (
            summary.data!.top_services_today.map((s) => (
              <TopSvcStrip key={s.service_name} svc={s} />
            ))
          )}
        </Panel>

        {/* Runtime observability — process-level counters */}
        <RuntimePanel />

        {/* vLLM KV cache + scheduler — per active inference instance */}
        <VLLMPanel />

        {/* System status — collapsible (was the entire v2 dashboard) */}
        <CollapsibleSystem open={sysOpen} onToggle={() => setSysOpen((v) => !v)} />
      </div>
    </div>
  )
}

// ---------- top-level pieces ----------

function BizStat({
  label,
  value,
  sub,
  valueColor,
  subColor,
}: {
  label: string
  value: string
  sub: string
  valueColor?: string
  subColor?: string
}) {
  return (
    <div
      style={{
        background: 'var(--bg-accent)',
        border: '1px solid var(--border)',
        borderRadius: 8,
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
          color: valueColor ?? 'var(--text)',
          marginTop: 4,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </div>
      {sub && (
        <div
          style={{ fontSize: 11, color: subColor ?? 'var(--muted)', marginTop: 4 }}
        >
          {sub}
        </div>
      )}
    </div>
  )
}

function Panel({
  icon,
  title,
  rightSlot,
  children,
}: {
  icon: React.ReactNode
  title: string
  rightSlot?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div
      style={{
        background: 'var(--bg-accent)',
        border: '1px solid var(--border)',
        borderRadius: 8,
        padding: '14px 16px',
        marginBottom: 12,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 10,
        }}
      >
        {icon}
        <h3 style={{ fontSize: 13, color: 'var(--text)', fontWeight: 500, flex: 1 }}>
          {title}
        </h3>
        {rightSlot}
      </div>
      {children}
    </div>
  )
}

// 运行时观测：进程内累计的 gzip 压缩比 / context 压缩 / cache 命中率。
// 重启清零；想要持久化跨重启数据走 Prometheus，本卡片只看"现在跑得咋样"。
function RuntimePanel() {
  const { data, isLoading, error } = useRuntimeMetrics()
  const subtitle = isLoading
    ? '加载中…'
    : error
      ? '指标接口不可达'
      : data
        ? `进程累计：${data.gzip.calls} 次压缩 · ${data.compaction.calls} 次 compaction · ${data.cache.lookups} 次 cache 查询`
        : '进程刚启动，等首批请求'

  return (
    <Panel
      icon={<Activity size={14} style={{ color: 'var(--info, #3b82f6)' }} />}
      title="运行时观测"
      rightSlot={
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{subtitle}</span>
      }
    >
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(3, 1fr)',
          gap: 10,
        }}
      >
        <RuntimeStat
          label="gzip 压缩比"
          value={fmtRatio(data?.gzip.compression_ratio)}
          hint={
            data?.gzip.calls
              ? `${fmtBytes(data.gzip.raw_bytes)} → ${fmtBytes(data.gzip.compressed_bytes)}`
              : '尚无数据'
          }
          tone={ratioTone(data?.gzip.compression_ratio)}
        />
        <RuntimeStat
          label="平均丢弃 turn"
          value={
            data?.compaction.avg_turns_dropped != null
              ? data.compaction.avg_turns_dropped.toFixed(1)
              : '—'
          }
          hint={
            data
              ? `${data.compaction.calls} 次 · ${data.compaction.truncated} 仍超限`
              : '尚无数据'
          }
          tone={compactionTone(data)}
        />
        <RuntimeStat
          label="cache 命中率"
          value={
            data?.cache.hit_rate != null
              ? `${(data.cache.hit_rate * 100).toFixed(1)}%`
              : '—'
          }
          hint={
            data?.cache.lookups
              ? `${data.cache.hits} hits / ${data.cache.lookups} lookups`
              : '尚无数据'
          }
          tone={hitRateTone(data?.cache.hit_rate)}
        />
      </div>
    </Panel>
  )
}

function RuntimeStat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint: string
  tone: 'ok' | 'warn' | 'neutral'
}) {
  const valueColor =
    tone === 'ok'
      ? 'var(--accent-2, #22c55e)'
      : tone === 'warn'
        ? 'var(--warn, #f59e0b)'
        : 'var(--text)'
  return (
    <div
      style={{
        background: 'var(--bg)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '10px 12px',
      }}
    >
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 20, color: valueColor, fontWeight: 600 }}>{value}</div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>{hint}</div>
    </div>
  )
}

function fmtRatio(r: number | null | undefined): string {
  if (r == null) return '—'
  return `${r.toFixed(2)}×`
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

function ratioTone(r: number | null | undefined): 'ok' | 'warn' | 'neutral' {
  if (r == null) return 'neutral'
  if (r < 1.5) return 'warn' // 压缩 < 1.5x 说明 gzip 几乎没帮上忙
  return 'ok'
}

function compactionTone(d: RuntimeSnapshot | undefined): 'ok' | 'warn' | 'neutral' {
  if (!d || d.compaction.calls === 0) return 'neutral'
  // 任何 truncated 都是问题（说明 max_tokens 卡得太紧）
  if (d.compaction.truncated > 0) return 'warn'
  return 'ok'
}

function hitRateTone(r: number | null | undefined): 'ok' | 'warn' | 'neutral' {
  if (r == null) return 'neutral'
  // 命中率 < 30% 提示 cache 可能没在用对
  return r >= 0.3 ? 'ok' : 'warn'
}

function AlertRow({ alert }: { alert: AlertItem }) {
  const dotColor =
    alert.severity === 'err' ? 'var(--accent, #ef4444)' : 'var(--warn, #f59e0b)'
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '8px 0',
        borderBottom: '1px solid var(--border)',
        fontSize: 12,
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: dotColor,
          flexShrink: 0,
        }}
      />
      <div style={{ flex: 1, color: 'var(--text)' }}>
        <strong style={{ color: 'var(--text)' }}>
          {alert.service_name ?? '(unknown)'}
        </strong>{' '}
        <span style={{ color: 'var(--muted)' }}>
          触发 {alert.threshold_percent}% 阈值
        </span>
      </div>
      <span style={{ color: 'var(--muted)', fontSize: 11 }}>
        {fmtTimeAgo(alert.last_notified_at)}
      </span>
    </div>
  )
}

function TopSvcStrip({ svc }: { svc: TopServiceRow }) {
  const pct = Math.max(0, Math.min(100, svc.percent))
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        padding: '10px 0',
        borderBottom: '1px solid var(--border)',
        fontSize: 12,
      }}
    >
      <div
        style={{
          width: 140,
          color: 'var(--text)',
          fontWeight: 500,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {svc.service_name}
      </div>
      <div
        style={{
          flex: 1,
          height: 6,
          background: 'var(--border)',
          borderRadius: 3,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: '100%',
            background: 'var(--accent-2, #22c55e)',
          }}
        />
      </div>
      <div
        style={{
          color: 'var(--muted)',
          fontVariantNumeric: 'tabular-nums',
          width: 80,
          textAlign: 'right',
        }}
      >
        {fmtN(svc.calls)} calls
      </div>
      <div
        style={{
          color: 'var(--muted)',
          fontVariantNumeric: 'tabular-nums',
          width: 50,
          textAlign: 'right',
        }}
      >
        {pct.toFixed(0)}%
      </div>
    </div>
  )
}

// ---------- collapsible system status (the v2 dashboard, folded down) ----------

function CollapsibleSystem({
  open,
  onToggle,
}: {
  open: boolean
  onToggle: () => void
}) {
  const { data: gpuData } = useSysGpus()
  const { data: engines } = useEngines()
  const { data: sysStats } = useSysStats()
  const { data: procData } = useSysProcesses()
  const [procSort, setProcSort] = useState<{ key: ProcSortKey; dir: ProcSortDir }>({ key: 'cpu', dir: 'desc' })
  const toggleProcSort = (key: ProcSortKey) =>
    setProcSort((s) => (s.key === key ? { key, dir: s.dir === 'desc' ? 'asc' : 'desc' } : { key, dir: 'desc' }))
  const sortedProcs = procData ? sortProcesses(procData.processes, procSort.key, procSort.dir) : []
  const netProbeOk = procData?.net_probe?.available ?? false
  const sortMark = (key: ProcSortKey) => (procSort.key === key ? (procSort.dir === 'desc' ? ' ▼' : ' ▲') : '')
  const thBtn: CSSProperties = { padding: '4px 8px', cursor: 'pointer', userSelect: 'none' }
  const killProcess = useKillProcess()
  const { data: runners } = useRunners()
  // image/tts adapter 真加载在 runner 子进程,主进程 engines 看不到 → 必须聚合
  // /loaded-adapters(各 runner Pong 快照)。否则 Dashboard 系统状态恒显「0 模型常驻 /
  // 已加载 (0)」而 GPU 卡明明有 Runner: Image 占显存(Bug 3 在 Dashboard 分支的残留;
  // ModelsOverlay 已用 useLoadedAdapters 聚合,本组件没跟上)。
  const { data: loadedAdaptersData } = useLoadedAdapters()
  // GPU 组（configs/hardware.yaml 声明、经 /api/v1/gpu/groups 下发）。返回空 = 本机没有
  // 声明过多卡组 → 下面的 layout 退化成「每张物理卡一张卡片」，与改造前逐字一致。
  const { data: groupData } = useGpuGroups()

  const loadedModels = (engines ?? []).filter((e) => e.status === 'loaded')
  const loadedAdapters = loadedAdaptersData?.entries ?? []
  const totalLoaded = loadedModels.length + loadedAdapters.length

  const cards = layoutGpuCards(gpuData?.gpus ?? [], groupData?.groups ?? [])
  const groupCount = cards.filter((c) => c.kind === 'group').length
  const subtitle =
    `${gpuData?.gpus.length ?? 0}× GPU` +
    (groupCount > 0 ? `（${groupCount} 组）` : '') +
    ` · ${totalLoaded} 模型常驻`

  const fmt = (n: number, d = 1) => n.toFixed(d)

  const onKill = async (pid: number, mem: number) => {
    if (
      await confirmDialog({
        message: `结束进程 PID ${pid}?将释放约 ${(mem / 1024).toFixed(1)}G 显存。`,
        danger: true, confirmText: 'Kill',
      })
    ) {
      killProcess.mutate(pid)
    }
  }
  const runnerOf = (indices: number[]) =>
    (runners ?? []).find((r) => r.gpus.some((g) => indices.includes(g))) ?? null

  return (
    <div
      style={{
        background: 'var(--bg-accent)',
        border: '1px solid var(--border)',
        borderRadius: 8,
        marginBottom: 12,
        overflow: 'hidden',
      }}
    >
      <button
        type="button"
        onClick={onToggle}
        style={{
          padding: '12px 16px',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          width: '100%',
          background: 'transparent',
          border: 'none',
          borderBottom: open ? '1px solid var(--border)' : 'none',
          cursor: 'pointer',
          color: 'var(--text)',
          textAlign: 'left',
        }}
      >
        <ChevronRight
          size={14}
          style={{
            transform: open ? 'rotate(90deg)' : 'rotate(0)',
            transition: 'transform 0.15s',
          }}
        />
        <Cpu size={14} style={{ color: 'var(--muted)' }} />
        <h3 style={{ fontSize: 13, color: 'var(--text)', fontWeight: 500, flex: 1 }}>
          系统状态
        </h3>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{subtitle}</span>
      </button>

      {open && (
        <div style={{ padding: '14px 16px' }}>
          {/* GPU panels */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
              gap: 12,
              marginBottom: 14,
            }}
          >
            {cards.map((card) =>
              card.kind === 'group' ? (
                <GpuGroupCard
                  key={`grp-${card.group.id ?? card.gpus.map((g) => g.index).join('+')}`}
                  group={card.group}
                  gpus={card.gpus}
                  runner={runnerOf(card.gpus.map((g) => g.index))}
                  onKill={onKill}
                />
              ) : (
                <GpuCard
                  key={card.gpu.index}
                  gpu={card.gpu}
                  runner={runnerOf([card.gpu.index])}
                  onKill={onKill}
                />
              ),
            )}
            {!gpuData && (
              <div style={{ fontSize: 11, color: 'var(--muted)', padding: 16 }}>
                等待 GPU 数据…
              </div>
            )}
          </div>

          {/* mini system stats */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(4, 1fr)',
              gap: 10,
              marginBottom: 14,
            }}
          >
            <MiniStat
              label="CPU"
              value={sysStats ? `${fmt(sysStats.cpu_usage_percent, 0)}%` : '—'}
              hint={sysStats ? `${sysStats.cpu_count} cores` : ''}
            />
            <MiniStat
              label="RAM"
              value={sysStats ? `${fmt(sysStats.memory_used_gb)}G` : '—'}
              hint={sysStats ? `/ ${fmt(sysStats.memory_total_gb)}G` : ''}
            />
            <MiniStat
              label="SWAP"
              value={sysStats ? `${fmt(sysStats.swap_used_gb)}G` : '—'}
              hint={sysStats ? `/ ${fmt(sysStats.swap_total_gb)}G` : ''}
            />
            <MiniStat
              label="DISK"
              value={sysStats ? `${fmt(sysStats.disk_used_gb)}G` : '—'}
              hint={sysStats ? `/ ${fmt(sysStats.disk_total_gb)}G` : ''}
            />
          </div>

          {/* loaded models */}
          <div
            style={{
              fontSize: 11,
              color: 'var(--muted)',
              textTransform: 'uppercase',
              letterSpacing: 0.5,
              margin: '4px 0 6px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ flex: 1 }}>已加载模型 ({totalLoaded})</span>
              <UnloadImageAdaptersButton />
            </div>
          </div>
          {totalLoaded === 0 && (
            <div style={{ fontSize: 12, color: 'var(--muted)', padding: '4px 0' }}>
              无常驻模型
            </div>
          )}
          {loadedModels.map((m) => (
            <div
              key={m.name}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '6px 0',
                fontSize: 12,
              }}
            >
              <span
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: '50%',
                  background: 'var(--ok, #22c55e)',
                }}
              />
              <span style={{ color: 'var(--text)' }}>{m.name}</span>
              <span
                style={{
                  fontSize: 10,
                  padding: '1px 6px',
                  borderRadius: 3,
                  background: 'rgba(34,197,94,0.15)',
                  color: 'var(--accent-2, #22c55e)',
                }}
              >
                {m.type.toUpperCase()}
              </span>
              {m.loaded_gpus && m.loaded_gpus.length > 0 && (
                <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontSize: 11 }}>
                  GPU {m.loaded_gpus.join('+')} · {m.vram_gb}GB
                </span>
              )}
            </div>
          ))}
          {/* runner 子进程加载的 image/tts adapter(主进程 engines 看不到,聚合 Pong 快照)*/}
          {loadedAdapters.map((a) => (
            <div
              key={a.model_id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '6px 0',
                fontSize: 12,
              }}
            >
              <span
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: '50%',
                  background: 'var(--ok, #22c55e)',
                }}
              />
              <span style={{ color: 'var(--text)' }}>{a.display_name}</span>
              <span
                style={{
                  fontSize: 10,
                  padding: '1px 6px',
                  borderRadius: 3,
                  background: 'rgba(34,197,94,0.15)',
                  color: 'var(--accent-2, #22c55e)',
                }}
              >
                {a.model_type.toUpperCase()}
              </span>
              <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontSize: 11 }}>
                {a.gpu_index != null ? `GPU ${a.gpu_index}` : ''}
                {a.vram_mb != null ? ` · ${(a.vram_mb / 1024).toFixed(1)}GB` : ''}
              </span>
            </div>
          ))}

          {/* process table */}
          <div
            style={{
              fontSize: 11,
              color: 'var(--muted)',
              textTransform: 'uppercase',
              letterSpacing: 0.5,
              margin: '14px 0 6px',
            }}
          >
            进程 ({procData?.processes.length ?? 0})
          </div>
          {procData?.processes ? (
            <div style={{ overflowX: 'auto' }}>
              <table
                style={{
                  width: '100%',
                  borderCollapse: 'collapse',
                  fontSize: 11,
                  fontFamily: 'var(--mono, monospace)',
                }}
              >
                <thead>
                  <tr style={{ color: 'var(--muted)', textAlign: 'left' }}>
                    <th style={{ padding: '4px 8px' }}>PID</th>
                    <th style={thBtn} onClick={() => toggleProcSort('cpu')}>CPU%{sortMark('cpu')}</th>
                    <th style={thBtn} onClick={() => toggleProcSort('mem')}>MEM{sortMark('mem')}</th>
                    <th
                      style={thBtn}
                      onClick={() => toggleProcSort('net')}
                      title={netProbeOk ? '每进程 TCP/UDP 收发速率(采集器 nous-engine-netprobe)' : '采集器未运行:sudo ./infra/systemd/install.sh'}
                    >
                      网络{sortMark('net')}
                    </th>
                    <th style={{ padding: '4px 8px' }}>NAME</th>
                    <th style={{ padding: '4px 8px' }}>COMMAND</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedProcs.slice(0, 15).map((p) => (
                    <tr key={p.pid} style={{ borderTop: '1px solid var(--border)' }}>
                      <td style={{ padding: '3px 8px', color: 'var(--muted)' }}>{p.pid}</td>
                      <td style={{ padding: '3px 8px', color: 'var(--muted)' }}>
                        {p.cpu_percent.toFixed(1)}%
                      </td>
                      <td style={{ padding: '3px 8px', color: 'var(--warn, #f59e0b)' }}>
                        {p.memory_mb}M
                      </td>
                      <td style={{ padding: '3px 8px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                        {p.net_tx_bps === null ? '—' : `▲ ${formatRate(p.net_tx_bps)} ▼ ${formatRate(p.net_rx_bps)}`}
                      </td>
                      <td style={{ padding: '3px 8px', color: 'var(--muted)' }}>{p.name}</td>
                      <td
                        style={{
                          padding: '3px 8px',
                          color: 'var(--accent-2, #22c55e)',
                          maxWidth: 600,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {p.command}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ fontSize: 11, color: 'var(--muted)', padding: 8 }}>
              启动 nous-center-sys 以获取进程数据
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function MiniStat({
  label,
  value,
  hint,
}: {
  label: string
  value: string
  hint?: string
}) {
  return (
    <div
      style={{
        background: 'var(--bg)',
        padding: '10px 12px',
        borderRadius: 4,
      }}
    >
      <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 14,
          fontWeight: 500,
          color: 'var(--text)',
          fontVariantNumeric: 'tabular-nums',
          marginTop: 2,
        }}
      >
        {value}
        {hint && (
          <span style={{ color: 'var(--muted)', fontSize: 10, marginLeft: 6 }}>
            {hint}
          </span>
        )}
      </div>
    </div>
  )
}

// ---------- GPU 组合并视图 ----------

type GpuCardItem =
  | { kind: 'single'; gpu: SysGpuInfo }
  | { kind: 'group'; group: GpuGroup; gpus: SysGpuInfo[] }

/**
 * 把「每卡一条实时指标」按 GPU 组折叠成卡片列表。
 *
 * 组的权威来源只有 `/api/v1/gpu/groups`（= configs/hardware.yaml），前端**绝不**自己
 * 按型号/NVLink 拼组 —— 那份 yaml 里记着运维约束（GPU 0 驱动显示器等）。这里只做展示
 * 折叠，不参与任何放置决策。
 *
 * 规则：① 只认 ≥2 张的组；② 组内的卡必须都在本次指标里（少一张就整组降级成单卡，
 * 免得画一张缺卡的合并卡去误导容量）；③ 一张卡只能被一个组吃掉，先到先得（yaml 理论上
 * 可以声明重叠的组）；④ 剩下的卡照旧单独一张。返回顺序按最小卡号排，跟物理序一致。
 */
function layoutGpuCards(gpus: SysGpuInfo[], groups: GpuGroup[]): GpuCardItem[] {
  const byIndex = new Map(gpus.map((g) => [g.index, g]))
  const claimed = new Set<number>()
  const items: { sort: number; item: GpuCardItem }[] = []

  for (const group of groups) {
    const indices = [...new Set(group.gpus)].sort((a, b) => a - b)
    if (indices.length < 2) continue
    if (indices.some((i) => claimed.has(i) || !byIndex.has(i))) continue
    indices.forEach((i) => claimed.add(i))
    items.push({
      sort: indices[0],
      item: { kind: 'group', group, gpus: indices.map((i) => byIndex.get(i)!) },
    })
  }
  for (const gpu of gpus) {
    if (claimed.has(gpu.index)) continue
    items.push({ sort: gpu.index, item: { kind: 'single', gpu } })
  }
  return items.sort((a, b) => a.sort - b.sort).map((e) => e.item)
}

interface MergedProc {
  key: string
  label: string
  managed: boolean
  /** 合计显存 MB。 */
  totalMb: number
  /** 每卡分解，按卡号排。 */
  parts: { gpu: number; mb: number; pid: number }[]
  /** 单条 orphan 用得上（kill 按钮要 pid）。 */
  proc: GpuProcessInfo
}

/**
 * 组内进程合并：同一个 managed 模型（tp>1 时是每卡各一个 pid）合成一行，显存合计 +
 * 每卡分解；orphan（桌面程序之类）照旧逐条列，只额外标出它在哪张卡上。
 *
 * tp 不是后端字段，就用「同名 managed 进程落在组内几张卡上」推断 —— 这正是 tp 的定义，
 * 不必为了显示再往 /monitor/stats 里加字段。
 */
function mergeGroupProcesses(gpus: SysGpuInfo[]): MergedProc[] {
  const merged = new Map<string, MergedProc>()
  const orphans: MergedProc[] = []
  for (const gpu of gpus) {
    for (const proc of gpu.processes ?? []) {
      // proc.gpu 理论上就是 gpu.index，但以外层卡为准更稳（旧 payload 有过 gpu=0 兜底）。
      const part = { gpu: gpu.index, mb: proc.used_gpu_memory_mb, pid: proc.pid }
      if (!proc.managed) {
        orphans.push({
          key: `orphan-${proc.pid}`,
          label: proc.command || proc.name,
          managed: false,
          totalMb: proc.used_gpu_memory_mb,
          parts: [part],
          proc,
        })
        continue
      }
      const key = proc.model_name || proc.name || String(proc.pid)
      const hit = merged.get(key)
      if (hit) {
        hit.totalMb += proc.used_gpu_memory_mb
        hit.parts.push(part)
      } else {
        merged.set(key, {
          key: `managed-${key}`,
          label: key,
          managed: true,
          totalMb: proc.used_gpu_memory_mb,
          parts: [part],
          proc,
        })
      }
    }
  }
  const managed = [...merged.values()]
  managed.forEach((m) => m.parts.sort((a, b) => a.gpu - b.gpu))
  orphans.sort((a, b) => a.parts[0].gpu - b.parts[0].gpu || a.proc.pid - b.proc.pid)
  return [...managed, ...orphans]
}

const g1 = (mb: number) => (mb / 1024).toFixed(1)

/**
 * 一个 GPU 组画成一张大卡 —— 用户对 NVLink 的 3090 对的心智模型就是「一张 48G 卡」。
 * 显存合计、利用率取组内最大（tp 下各卡基本同步，最大值最接近「这组忙不忙」），
 * 温度/风扇/功耗逐卡一行小字（合计没有物理意义，平均会藏掉一张卡过热）。
 * 右上角「展开物理卡」把原来的单卡卡片原样铺在下面，默认折叠。
 */
function GpuGroupCard({
  group,
  gpus,
  runner,
  onKill,
}: {
  group: GpuGroup
  gpus: SysGpuInfo[]
  runner: RunnerInfo | null
  onKill: (pid: number, mem: number) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const indices = gpus.map((g) => g.index)
  const usedMb = gpus.reduce((s, g) => s + g.memory_used_mb, 0)
  const totalMb = gpus.reduce((s, g) => s + g.memory_total_mb, 0)
  const memPct = totalMb > 0 ? (usedMb / totalMb) * 100 : 0
  const maxUtil = Math.max(...gpus.map((g) => g.utilization_gpu))
  const lowMemory = gpus.some((g) => g.low_memory)
  const procs = mergeGroupProcesses(gpus)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div
        style={{
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          borderRadius: 6,
          padding: '12px 14px',
        }}
      >
        <div
          style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'space-between' }}
        >
          <span
            style={{
              fontSize: 11,
              fontWeight: 600,
              color: 'var(--accent-2, #22c55e)',
              letterSpacing: 0.5,
            }}
          >
            GPU {indices.join('+')}
            {group.id ? ` · ${group.id}` : ''}
          </span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {group.nvlink && (
              <span
                style={{
                  fontSize: 9,
                  padding: '1px 5px',
                  borderRadius: 3,
                  background: 'rgba(99,102,241,0.15)',
                  color: 'var(--accent, #6366f1)',
                  letterSpacing: 0.5,
                }}
              >
                NVLink
              </span>
            )}
            <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--text)' }}>
              {maxUtil}%
            </span>
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              style={{
                fontSize: 10,
                padding: '2px 8px',
                borderRadius: 3,
                background: 'transparent',
                color: 'var(--muted)',
                border: '1px solid var(--border)',
                cursor: 'pointer',
              }}
            >
              {expanded ? '收起物理卡' : '展开物理卡'}
            </button>
          </div>
        </div>
        <div style={{ fontSize: 13, color: 'var(--text)', marginTop: 4 }}>
          {group.name} ×{gpus.length}
        </div>
        {runner && (
          <div style={{ fontSize: 11, color: 'var(--info, #3b82f6)', marginTop: 2 }}>
            Runner: {runner.label} ({runner.role})
          </div>
        )}
        <div style={{ marginTop: 4, marginBottom: 10 }}>
          {gpus.map((g) => (
            <div
              key={g.index}
              style={{ fontSize: 11, color: 'var(--muted)', display: 'flex', gap: 12 }}
            >
              <span style={{ minWidth: 24 }}>#{g.index}</span>
              <span style={{ color: 'var(--accent-2, #22c55e)' }}>{g.temperature}°C</span>
              <span>FAN {g.fan_speed}%</span>
              <span>
                POW {g.power_draw_w.toFixed(0)}/{g.power_limit_w.toFixed(0)}W
              </span>
            </div>
          ))}
        </div>
        <BarRow label="GPU" value={`${maxUtil}%`} pct={maxUtil} />
        <div
          style={{ fontSize: 10, color: 'var(--muted)', margin: '0 0 2px 42px' }}
        >
          {gpus.map((g) => `#${g.index} ${g.utilization_gpu}%`).join(' · ')}
        </div>
        <BarRow
          label="MEM"
          value={`${g1(usedMb)}G / ${g1(totalMb)}G`}
          pct={memPct}
          warn={lowMemory}
        />
        {procs.length > 0 && (
          <div
            style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed var(--border)' }}
          >
            <div
              style={{
                fontSize: 9,
                fontWeight: 600,
                color: 'var(--muted)',
                textTransform: 'uppercase',
                letterSpacing: 0.5,
                marginBottom: 6,
              }}
            >
              GPU Processes
            </div>
            {procs.map((m) =>
              m.managed ? (
                <MergedProcRow key={m.key} merged={m} />
              ) : (
                <ProcRow
                  key={m.key}
                  proc={m.proc}
                  gpuTag={`#${m.parts[0].gpu}`}
                  onKill={onKill}
                />
              ),
            )}
          </div>
        )}
      </div>
      {expanded &&
        gpus.map((g) => (
          <GpuCard key={g.index} gpu={g} runner={runner} onKill={onKill} />
        ))}
    </div>
  )
}

/** 组内 managed 进程合并成的一行：合计显存 + 每卡分解 + tp 徽标。 */
function MergedProcRow({ merged }: { merged: MergedProc }) {
  const tp = new Set(merged.parts.map((p) => p.gpu)).size
  const breakdown = merged.parts.map((p) => `#${p.gpu} ${g1(p.mb)}G`).join(' · ')
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '3px 0',
        fontSize: 11,
        fontFamily: 'var(--mono, monospace)',
        color: 'var(--muted)',
      }}
    >
      <span
        title={merged.parts.map((p) => `#${p.gpu} PID ${p.pid}`).join('\n')}
        style={{ minWidth: 92, cursor: 'help' }}
      >
        {g1(merged.totalMb)}G
        <span style={{ marginLeft: 4 }}>({breakdown})</span>
      </span>
      <span
        title={merged.proc.command_full || merged.proc.command || undefined}
        style={{
          flex: 1,
          color: 'var(--text)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          cursor: 'help',
        }}
      >
        {merged.label}
      </span>
      {tp > 1 && (
        <span
          style={{
            fontSize: 9,
            padding: '1px 5px',
            borderRadius: 3,
            background: 'rgba(99,102,241,0.15)',
            color: 'var(--accent, #6366f1)',
          }}
        >
          tp={tp}
        </span>
      )}
      <span
        style={{
          fontSize: 9,
          padding: '1px 5px',
          borderRadius: 3,
          background: 'rgba(34,197,94,0.15)',
          color: 'var(--accent-2, #22c55e)',
        }}
      >
        managed
      </span>
    </div>
  )
}

function GpuCard({
  gpu,
  runner,
  onKill,
}: {
  gpu: SysGpuInfo
  runner: RunnerInfo | null
  onKill: (pid: number, mem: number) => void
}) {
  const memPct =
    gpu.memory_total_mb > 0 ? (gpu.memory_used_mb / gpu.memory_total_mb) * 100 : 0
  return (
    <div
      style={{
        background: 'var(--bg)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '12px 14px',
      }}
    >
      <div
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
      >
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: 'var(--accent-2, #22c55e)',
            letterSpacing: 0.5,
          }}
        >
          GPU {gpu.index}
        </span>
        <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--text)' }}>
          {gpu.utilization_gpu}%
        </span>
      </div>
      <div style={{ fontSize: 13, color: 'var(--text)', marginTop: 4 }}>{gpu.name}</div>
      {runner && (
        <div style={{ fontSize: 11, color: 'var(--info, #3b82f6)', marginTop: 2 }}>
          Runner: {runner.label} ({runner.role})
        </div>
      )}
      <div
        style={{
          fontSize: 11,
          color: 'var(--muted)',
          display: 'flex',
          gap: 12,
          marginTop: 4,
          marginBottom: 10,
        }}
      >
        <span style={{ color: 'var(--accent-2, #22c55e)' }}>{gpu.temperature}°C</span>
        <span>FAN {gpu.fan_speed}%</span>
        <span>POW {gpu.power_draw_w.toFixed(0)}/{gpu.power_limit_w.toFixed(0)}W</span>
      </div>
      <BarRow label="GPU" value={`${gpu.utilization_gpu}%`} pct={gpu.utilization_gpu} />
      <BarRow
        label="MEM"
        value={`${(gpu.memory_used_mb / 1024).toFixed(1)}G / ${(gpu.memory_total_mb / 1024).toFixed(0)}G`}
        pct={memPct}
        warn={gpu.low_memory}
      />
      {gpu.processes && gpu.processes.length > 0 && (
        <div
          style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed var(--border)' }}
        >
          <div
            style={{
              fontSize: 9,
              fontWeight: 600,
              color: 'var(--muted)',
              textTransform: 'uppercase',
              letterSpacing: 0.5,
              marginBottom: 6,
            }}
          >
            GPU Processes
          </div>
          {gpu.processes.map((proc) => (
            <ProcRow key={proc.pid} proc={proc} onKill={onKill} />
          ))}
        </div>
      )}
    </div>
  )
}

function BarRow({
  label,
  value,
  pct,
  warn,
}: {
  label: string
  value: string
  pct: number
  warn?: boolean
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '4px 0',
        fontSize: 11,
      }}
    >
      <span
        style={{
          width: 32,
          fontSize: 10,
          color: 'var(--muted)',
          textTransform: 'uppercase',
        }}
      >
        {label}
      </span>
      <div
        style={{
          flex: 1,
          height: 4,
          background: 'var(--border)',
          borderRadius: 2,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${Math.min(100, pct)}%`,
            height: '100%',
            background: warn ? 'var(--warn, #f59e0b)' : 'var(--accent-2, #22c55e)',
          }}
        />
      </div>
      <span
        style={{
          color: 'var(--muted)',
          fontVariantNumeric: 'tabular-nums',
          minWidth: 70,
          textAlign: 'right',
        }}
      >
        {value}
      </span>
    </div>
  )
}

/** PR-D4:dashboard「系统状态」面板的「已加载模型」标题旁,
 * 一键释放所有 image adapter(走 `_models[derived_id]` 统一字典 + `empty_cache`)。
 *
 * image adapter 由 workflow 节点动态组装,combo_key 唯一 — 用户改 dtype/LoRA 多次
 * Run 后会累积多份(72GB 报告就是这样)。LRU 自动驱逐只在 OOM 时触发;用户想主动
 * 清空 cuda:1 跑下一张时用这按钮。空状态也提示 toast,不报错。 */
function UnloadImageAdaptersButton() {
  const unload = useUnloadImageAdapters()
  return (
    <button
      type="button"
      onClick={() => unload.mutate()}
      disabled={unload.isPending}
      style={{
        fontSize: 10,
        padding: '2px 8px',
        borderRadius: 3,
        background: 'transparent',
        color: 'var(--warn, #f59e0b)',
        border: '1px solid var(--warn, #f59e0b)',
        cursor: unload.isPending ? 'wait' : 'pointer',
        opacity: unload.isPending ? 0.5 : 1,
      }}
      title="释放所有 image adapter 显存"
    >
      {unload.isPending ? '释放中…' : '释放 image'}
    </button>
  )
}

function ProcRow({
  proc,
  onKill,
  /** 合并卡里标明这条进程在组内哪张卡上（`#0`）；单卡卡片不传，渲染与改造前一致。 */
  gpuTag,
}: {
  proc: GpuProcessInfo
  onKill: (pid: number, mem: number) => void
  gpuTag?: string
}) {
  const memG = (proc.used_gpu_memory_mb / 1024).toFixed(1)
  const isOrphan = !proc.managed
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '3px 0',
        fontSize: 11,
        fontFamily: 'var(--mono, monospace)',
        color: isOrphan ? 'var(--warn, #f59e0b)' : 'var(--muted)',
      }}
    >
      {gpuTag && <span style={{ minWidth: 22 }}>{gpuTag}</span>}
      <span style={{ minWidth: 48 }}>{proc.pid}</span>
      <span style={{ minWidth: 44 }}>{memG}G</span>
      <span
        // PR-10:hover 显示完整 cmdline(`command_full` 优先,旧 payload 没字段时
        // 降级用 `command`)。managed 进程则把完整 cmdline + model_name 都丢进 tooltip,
        // 让操作员知道这条 row 既是 model XXX 也是 process YYY,不用切到 `ps`。
        title={
          proc.command_full || proc.command
            ? proc.managed && proc.model_name
              ? `${proc.model_name}\n${proc.command_full || proc.command}`
              : (proc.command_full || proc.command)
            : undefined
        }
        style={{
          flex: 1,
          color: 'var(--text)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          cursor: 'help',
        }}
      >
        {proc.managed ? proc.model_name : proc.command}
      </span>
      {proc.managed ? (
        <span
          style={{
            fontSize: 9,
            padding: '1px 5px',
            borderRadius: 3,
            background: 'rgba(34,197,94,0.15)',
            color: 'var(--accent-2, #22c55e)',
          }}
        >
          managed
        </span>
      ) : (
        <>
          <span
            style={{
              fontSize: 9,
              padding: '1px 5px',
              borderRadius: 3,
              background: 'var(--warn, #f59e0b)',
              color: '#1a1a1a',
              fontFamily: 'inherit',
            }}
          >
            orphan
          </span>
          <button
            type="button"
            onClick={() => onKill(proc.pid, proc.used_gpu_memory_mb)}
            title={`Kill ${proc.pid}`}
            style={{
              width: 18,
              height: 18,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              borderRadius: 3,
              background: 'rgba(99,102,241,0.15)',
              color: 'var(--accent)',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            <X size={10} />
          </button>
        </>
      )}
    </div>
  )
}

// ---------- formatters ----------

function fmtN(n: number | undefined): string {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}K`
  return n.toLocaleString()
}

function deltaText(pct: number | null | undefined): string {
  if (pct == null) return '昨日无数据'
  const arrow = pct >= 0 ? '↑' : '↓'
  return `${arrow} ${Math.abs(pct).toFixed(1)}% vs 昨天`
}

function fmtTimeAgo(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const diffSec = (Date.now() - d.getTime()) / 1000
  if (diffSec < 60) return `${Math.floor(diffSec)} 秒前`
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`
  return `${Math.floor(diffSec / 86400)} 天前`
}

// vLLM KV cache + scheduler state — one card per active inference instance.
// Pulled from each instance's :PORT/metrics by the backend, polled every 3s.
function VLLMPanel() {
  const { data, isLoading, error } = useVLLMMetrics()
  const update = useUpdateLaunchParams()
  const instances = data?.instances ?? []

  const subtitle = isLoading
    ? '加载中…'
    : error
      ? '指标接口不可达'
      : instances.length === 0
        ? '没有正在运行的 vLLM 实例'
        : `${instances.length} 个实例 · 每 3 秒刷新`

  return (
    <Panel icon={<Cpu size={14} color="var(--accent)" />} title="vLLM KV Cache · 调度">
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 8 }}>{subtitle}</div>

      {instances.map((inst) => {
        const usage = inst.stats.kv_cache_usage_perc ?? 0
        const usagePct = Math.round(usage * 100)
        const totalBlocks = Number(inst.config.num_gpu_blocks ?? 0)
        const blockSize = Number(inst.config.block_size ?? 0)
        const totalTokens = totalBlocks * blockSize
        const prefixOn = inst.config.enable_prefix_caching === 'True'
        const hitRate = inst.stats.prefix_cache_hit_rate

        return (
          <div
            key={`${inst.name}-${inst.port}`}
            style={{
              border: '1px solid var(--border)',
              borderRadius: 6,
              padding: 12,
              marginBottom: 8,
              background: 'var(--bg)',
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                marginBottom: 8,
                flexWrap: 'wrap',
              }}
            >
              <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--text)' }}>
                {inst.name}
              </div>
              <div style={{ fontSize: 10, color: 'var(--muted)' }}>port :{inst.port}</div>
              {!inst.healthy && (
                <span style={{ fontSize: 10, color: 'var(--error, #ef4444)' }}>
                  · {inst.error}
                </span>
              )}
            </div>

            {inst.healthy && (
              <>
                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(4, 1fr)',
                    gap: 12,
                    marginBottom: 8,
                  }}
                >
                  <Stat label="KV 使用" value={`${usagePct}%`} />
                  <Stat
                    label="总容量"
                    value={
                      totalTokens > 0
                        ? `${(totalTokens / 1000).toFixed(0)}K tokens`
                        : '—'
                    }
                    sub={
                      totalBlocks > 0 && blockSize > 0
                        ? `${totalBlocks} blocks × ${blockSize}`
                        : undefined
                    }
                  />
                  <Stat
                    label="并发"
                    value={`${inst.stats.running ?? 0}`}
                    sub={
                      (inst.stats.waiting ?? 0) > 0
                        ? `等待 ${inst.stats.waiting}`
                        : undefined
                    }
                  />
                  <Stat
                    label="Prefix 命中"
                    value={
                      hitRate != null
                        ? `${Math.round(hitRate * 100)}%`
                        : prefixOn
                          ? '0%'
                          : '关'
                    }
                    sub={
                      prefixOn
                        ? `${inst.stats.prefix_cache_hits_total ?? 0} / ${inst.stats.prefix_cache_queries_total ?? 0}`
                        : undefined
                    }
                  />
                </div>

                {/* KV usage bar */}
                <div
                  style={{
                    height: 4,
                    borderRadius: 2,
                    background: 'var(--bg-accent)',
                    overflow: 'hidden',
                  }}
                >
                  <div
                    style={{
                      width: `${usagePct}%`,
                      height: '100%',
                      background:
                        usage > 0.85 ? 'var(--error, #ef4444)' : 'var(--accent)',
                      transition: 'width 0.3s',
                    }}
                  />
                </div>

                {/* Toggle: prefix caching (next-load apply) */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    marginTop: 12,
                    paddingTop: 10,
                    borderTop: '1px solid var(--border)',
                  }}
                >
                  <label
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 6,
                      fontSize: 11,
                      color: 'var(--text)',
                      cursor: 'pointer',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={prefixOn}
                      disabled={update.isPending}
                      onChange={(e) =>
                        update.mutate({
                          name: inst.name,
                          body: { enable_prefix_caching: e.target.checked },
                        })
                      }
                    />
                    Prefix Caching
                  </label>
                  <span style={{ fontSize: 10, color: 'var(--muted)' }}>
                    {update.isPending && '保存中…'}
                    {!update.isPending &&
                      '改后下次 load 生效（需 unload + load 重启 vLLM）'}
                  </span>
                </div>
              </>
            )}
          </div>
        )
      })}
    </Panel>
  )
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: 'var(--muted)', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 16,
          fontWeight: 600,
          color: 'var(--text)',
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 2 }}>{sub}</div>
      )}
    </div>
  )
}
