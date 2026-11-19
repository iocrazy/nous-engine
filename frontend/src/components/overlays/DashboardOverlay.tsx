import { useState, type ReactNode } from 'react'
import { Activity, AlertTriangle, BarChart3, Cpu, X } from 'lucide-react'
import {
  useSysGpus, useSysStats, useSysProcesses, useKillProcess,
  type SysGpuInfo, type GpuProcessInfo,
} from '../../api/system'
import {
  useEngines, useLoadedAdapters, useGpuGroups,
  type EngineInfo, type GpuGroup,
} from '../../api/engines'
import { useDashboardSummary, type AlertItem, type TopServiceRow } from '../../api/dashboard'
import { useRuntimeMetrics, type RuntimeSnapshot } from '../../api/observability'
import { useVLLMMetrics, type VLLMInstance } from '../../api/vllm'
import { useRunners, type RunnerInfo } from '../../api/runners'
import { useHealth } from '../../api/health'
import { useStatus, useStatusConnections, type ComponentStatus } from '../../api/status'
import { useComfyHealth, useFreeComfyCache } from '../../api/comfyTemplates'
import { useServices } from '../../api/services'
import { confirmDialog } from '../../stores/confirm'
import { sortProcesses, formatRate, type ProcSortKey, type ProcSortDir } from './processSort'

const TABS = [
  { id: 'overview', label: '概览' },
  { id: 'models', label: '模型' },
  { id: 'system', label: '系统状态' },
  { id: 'processes', label: '进程' },
  { id: 'usage', label: '调用与告警' },
] as const

type DashboardTab = typeof TABS[number]['id']
type ModelStatus = EngineInfo['status']

const MODEL_STATUS: Record<ModelStatus, { label: string; color: string; order: number }> = {
  failed: { label: '加载失败', color: 'var(--err, #ef4444)', order: 0 },
  loading: { label: '加载中', color: 'var(--warn, #f59e0b)', order: 1 },
  loaded: { label: '已加载', color: 'var(--ok, #22c55e)', order: 2 },
  unloaded: { label: '未加载', color: 'var(--muted)', order: 3 },
}

export default function DashboardOverlay() {
  const [tab, setTab] = useState<DashboardTab>('overview')
  const [statusFilter, setStatusFilter] = useState<ModelStatus | ''>('')
  const [modelSearch, setModelSearch] = useState('')
  const openModels = (status: ModelStatus | '' = '') => {
    setStatusFilter(status)
    setModelSearch('')
    setTab('models')
    document.getElementById('dashboard-tab-models')?.focus()
  }
  const openSystem = () => {
    setTab('system')
    document.getElementById('dashboard-tab-system')?.focus()
  }

  return (
    <div
      className="absolute inset-0 overflow-y-auto z-[16]"
      style={{ background: 'var(--bg)' }}
    >
      <div className="console-page console-controls">
        <div style={{ marginBottom: 14 }}>
          <h1 className="console-title">概览</h1>
          <p className="console-description">
            模型、系统资源与服务用量
          </p>
        </div>

        <div role="tablist" aria-label="概览视图" className="console-tabs">
          {TABS.map(({ id, label }, index) => (
            <button
              key={id}
              type="button"
              id={`dashboard-tab-${id}`}
              role="tab"
              aria-selected={tab === id}
              aria-controls="dashboard-panel"
              tabIndex={tab === id ? 0 : -1}
              className="console-tab"
              onClick={() => setTab(id)}
              onKeyDown={(event) => {
                const last = TABS.length - 1
                const destinations: Record<string, number> = {
                  ArrowLeft: index === 0 ? last : index - 1,
                  ArrowRight: index === last ? 0 : index + 1,
                  Home: 0,
                  End: last,
                }
                if (!Object.hasOwn(destinations, event.key)) return
                event.preventDefault()
                const next = TABS[destinations[event.key]].id
                setTab(next)
                document.getElementById(`dashboard-tab-${next}`)?.focus()
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <section id="dashboard-panel" role="tabpanel" tabIndex={0} aria-labelledby={`dashboard-tab-${tab}`}>
          {tab === 'overview' && <DashboardModels overview openModels={openModels} openSystem={openSystem} />}
          {tab === 'models' && (
            <DashboardModels
              statusFilter={statusFilter}
              modelSearch={modelSearch}
              setStatusFilter={setStatusFilter}
              setModelSearch={setModelSearch}
            />
          )}
          {tab === 'system' && <SystemStatus />}
          {tab === 'processes' && <SystemProcesses />}
          {tab === 'usage' && <DashboardUsage />}
        </section>
      </div>
    </div>
  )
}

function DashboardUsage() {
  const summary = useDashboardSummary()

  if (summary.isError) return <p role="alert">用量与告警加载失败</p>
  if (!summary.data) return <p role="status">正在加载用量与告警…</p>

  return (
    <>
      {/* Top business stats */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
          gap: 12,
          marginBottom: 12,
        }}
      >
        <BizStat
          label="今日已记录调用"
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
          label="近 30 天 Token"
          value={fmtN(summary.data?.month_tokens)}
          sub="从 30 天前的 UTC 零点起"
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

      <a className="text-sm underline" style={{ color: 'var(--accent)' }} href="/usage">查看完整用量</a>
    </>
  )
}

type DashboardModelsProps =
  | { overview: true; openModels: (status?: ModelStatus) => void; openSystem: () => void }
  | {
      overview?: false
      statusFilter: ModelStatus | ''
      modelSearch: string
      setStatusFilter: (status: ModelStatus | '') => void
      setModelSearch: (search: string) => void
    }

function DashboardModels(props: DashboardModelsProps) {
  const engines = useEngines()
  const adapters = useLoadedAdapters()
  const services = useServices()
  const serviceNames = new Map<string, Set<string>>()
  for (const service of services.data ?? []) {
    for (const model of service.models ?? []) {
      if (!model.engine_key) continue
      const names = serviceNames.get(model.engine_key) ?? new Set<string>()
      names.add(service.name)
      serviceNames.set(model.engine_key, names)
    }
  }

  const rows = [
    ...(engines.data ?? []).map((model) => ({
      id: `engine-${model.name}`,
      name: model.display_name || model.name,
      searchName: model.name,
      type: model.type,
      status: model.status,
      detail: model.status_detail,
      service: [...(serviceNames.get(model.name) ?? [])].join('、'),
      gpu: model.loaded_gpus?.length
        ? model.loaded_gpus.join('+')
        : model.loaded_gpu ?? model.gpus?.join('+') ?? model.gpu,
      policy: model.resident ? '常驻' : '按需',
    })),
    ...(adapters.data?.entries ?? []).map((model) => ({
      id: `adapter-${model.group_id}-${model.model_id}`,
      name: model.display_name,
      searchName: model.model_id,
      type: model.model_type,
      status: 'loaded' as const,
      detail: null,
      service: '',
      gpu: model.gpu_index,
      policy: '—',
    })),
  ].sort((a, b) => MODEL_STATUS[a.status].order - MODEL_STATUS[b.status].order || a.name.localeCompare(b.name))

  const term = props.overview ? '' : props.modelSearch.trim().toLowerCase()
  const visible = props.overview
    ? rows.filter((row) => row.status !== 'unloaded').slice(0, 8)
    : rows.filter((row) =>
        (!props.statusFilter || row.status === props.statusFilter) &&
        [row.name, row.searchName, row.service].some((value) => value.toLowerCase().includes(term)),
      )

  return (
    <>
      {props.overview && (
        <OverviewSummary rows={rows} modelCountsReady={!!engines.data && !!adapters.data} openModels={props.openModels} openSystem={props.openSystem} />
      )}
      <Panel icon={<Activity size={14} />} title={props.overview ? '模型运行状态' : '模型列表'}
        rightSlot={props.overview ? <button type="button" className="text-xs underline" onClick={() => props.openModels()}>查看全部模型</button> : undefined}
      >
        {!props.overview && (
          <div className="mb-3 flex flex-wrap gap-2">
            <input type="search" aria-label="搜索模型或服务" placeholder="搜索模型或服务"
              className="rounded border px-2 py-1 text-sm" value={props.modelSearch}
              onChange={(event) => props.setModelSearch(event.target.value)} />
            <select aria-label="模型状态" className="rounded border px-2 py-1 text-sm" value={props.statusFilter}
              onChange={(event) => props.setStatusFilter(event.target.value as ModelStatus | '')}>
              <option value="">全部状态</option>
              {Object.entries(MODEL_STATUS).map(([status, info]) => <option key={status} value={status}>{info.label}</option>)}
            </select>
          </div>
        )}
        {(engines.isError || adapters.isError) && <p role="alert" className="mb-2 text-xs">部分模型状态加载失败</p>}
        {services.isError && <p role="alert" className="mb-2 text-xs">关联服务加载失败</p>}
        {visible.length === 0 ? (
          <p className="py-2 text-xs" style={{ color: 'var(--muted)' }}>
            {engines.isLoading || adapters.isLoading ? '正在加载模型…' : '没有可显示的模型'}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="console-table" aria-label="模型运行状态">
              <thead style={{ color: 'var(--muted)' }}>
                <tr>{['模型', '状态', '服务', 'GPU', '加载策略'].map((label) => <th key={label} scope="col" className="px-2 py-2 font-medium">{label}</th>)}</tr>
              </thead>
              <tbody>
                {visible.map((model) => (
                  <tr key={model.id} className="border-t" style={{ borderColor: 'var(--border)' }}>
                    <td className="px-2 py-2.5">
                      <strong className="block font-medium">{model.name}</strong>
                      <span style={{ color: 'var(--muted)' }}>{model.type}</span>
                    </td>
                    <td className="px-2 py-2.5">
                      <span className="whitespace-nowrap" style={{ color: MODEL_STATUS[model.status].color }}>{MODEL_STATUS[model.status].label}</span>
                      {model.detail && <details className="mt-1 min-w-24">
                        <summary className="cursor-pointer">状态详情</summary>
                        <p className="max-w-xs whitespace-pre-wrap break-words">{model.detail}</p>
                      </details>}
                    </td>
                    <td className="px-2 py-2.5">{model.service || '—'}</td>
                    <td className="whitespace-nowrap px-2 py-2.5">{model.gpu != null ? `GPU ${model.gpu}` : '—'}</td>
                    <td className="whitespace-nowrap px-2 py-2.5">{model.policy}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  )
}

const HEALTH_LABEL: Record<ComponentStatus, string> = {
  operational: '正常',
  idle: '空闲',
  degraded: '降级',
  down: '中断',
}

function OverviewSummary({ rows, modelCountsReady, openModels, openSystem }: {
  rows: { status: ModelStatus }[]
  modelCountsReady: boolean
  openModels: (status: ModelStatus) => void
  openSystem: () => void
}) {
  const status = useStatus()
  const gpus = useSysGpus()
  const { data: system } = useSysStats()
  const attention = status.data?.components.filter((component) => component.status === 'degraded' || component.status === 'down')
  return (
    <>
      <Panel icon={<Cpu size={14} />} title="系统健康"
        rightSlot={<button type="button" className="text-xs underline" onClick={openSystem}>查看系统状态</button>}
      >
        <p className="text-xs" style={{ color: 'var(--muted)' }}>
          {status.isError ? '状态检查暂不可用' : attention?.length
            ? attention.map(({ name, status }) => `${name} ${HEALTH_LABEL[status]}`).join(' · ')
            : status.data ? `组件状态${HEALTH_LABEL[status.data.overall]}` : '正在检查组件状态…'}
          {system && <span className="ml-3">物理内存可用 {system.memory_available_gb.toFixed(1)} GiB</span>}
        </p>
      </Panel>
      <div className="mb-3 grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 180px), 1fr))' }}>
        {(['loaded', 'loading', 'failed'] as const).map((modelStatus) => (
          <button key={modelStatus} type="button" className="text-left" onClick={() => openModels(modelStatus)}>
            <BizStat label={MODEL_STATUS[modelStatus].label}
              value={modelCountsReady ? String(rows.filter((row) => row.status === modelStatus).length) : '—'}
              valueColor={MODEL_STATUS[modelStatus].color} sub="查看模型" />
          </button>
        ))}
        {(gpus.data?.gpus ?? []).map((gpu) => (
          <button key={gpu.index} type="button" className="text-left" onClick={openSystem}>
            <BizStat label={`GPU ${gpu.index} 剩余显存`} value={`${g1(gpu.memory_free_mb)} GiB`}
              sub={`总计 ${g1(gpu.memory_total_mb)} GiB`} />
          </button>
        ))}
        {!gpus.data && (
          <BizStat label="GPU 剩余显存" value="—" sub={gpus.isError ? '显存读数暂不可用' : '正在读取 GPU 状态'} />
        )}
      </div>
    </>
  )
}

function SystemConnections({ health }: { health: ReturnType<typeof useComfyHealth> }) {
  const status = useStatus()
  const connections = useStatusConnections()
  const sidecar = useHealth().data?.comfy
  const { data: processes } = useSysProcesses()
  const [copyMessage, setCopyMessage] = useState('')
  const bridge = health.data
  const probe = processes?.net_probe
  const render = bridge?.running_render
  const checks = connections.isError ? undefined : connections.data
  const startedAt = checks?.comfy_unit.started_at
  const restartCount = checks?.comfy_unit.restart_count
  const bridgeChecked = !health.isError && !!bridge

  const copyAddress = async (address: string) => {
    try {
      await navigator.clipboard.writeText(address)
      setCopyMessage('已复制地址')
    } catch {
      setCopyMessage('复制失败，可手动选中地址')
    }
  }

  return (
    <section aria-labelledby="dashboard-connections">
      <h2 id="dashboard-connections" className="mb-3 text-base font-semibold">服务与连接</h2>
      <div className="grid gap-x-3 md:grid-cols-2">
        <Panel icon={<Activity size={14} />} title="服务健康"
          rightSlot={<a href="/status" className="text-xs underline">过去 7 天可用率</a>}
        >
          {status.isError ? <p role="alert" className="text-xs">组件状态检查失败</p> : !status.data ? (
            <p role="status" className="text-xs">正在检查组件状态…</p>
          ) : status.data.components.map((component) => (
            <div key={component.key} className="flex flex-wrap justify-between gap-2 border-t py-2 text-xs" style={{ borderColor: 'var(--border)' }}>
              <span>{component.name}</span>
              <span>{HEALTH_LABEL[component.status]}</span>
              {component.detail && <p className="w-full" style={{ color: 'var(--muted)' }}>{component.detail}</p>}
            </div>
          ))}
          <div className="flex flex-wrap justify-between gap-2 border-t py-2 text-xs" style={{ borderColor: 'var(--border)' }}>
            <span>网络采集</span>
            <span>{probe ? (probe.available ? '采样正常' : '采样不可用') : '等待采样'}</span>
            {probe?.age_s != null && <p className="w-full" style={{ color: 'var(--muted)' }}>最近采样距今 {Math.round(probe.age_s)} 秒</p>}
          </div>
        </Panel>
        <Panel icon={<Activity size={14} />} title="访问入口">
          <div className="flex flex-wrap justify-between gap-2 text-xs">
            <span>后端 → ComfyUI</span>
            <span>{bridgeChecked ? (bridge.online ? '可达' : '不可达') : '状态未知'}</span>
          </div>
          {bridge?.base_url && (
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
              <code className="break-all text-xs">{bridge.base_url}</code>
              <button type="button" className="text-xs underline" onClick={() => void copyAddress(bridge.base_url)}>复制地址</button>
            </div>
          )}
          <div className="mt-3 border-t pt-2 text-xs" style={{ borderColor: 'var(--border)' }}>
            <p style={{ color: 'var(--muted)' }}>
              Tailscale · 服务器侧探测
              {checks && ` · ${new Date(checks.checked_at).toLocaleTimeString()}`}
            </p>
            {connections.isError && <p role="alert" className="mt-1">Tailscale 探测结果暂不可用</p>}
            {!checks && !connections.isError && <p role="status" className="mt-1">正在探测…</p>}
            {checks?.tailscale === null && <p className="mt-1">未配置有效的 Tailscale 地址</p>}
            {checks?.tailscale?.map((probe) => (
              <div key={probe.key} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div className="min-w-0">
                  <span className="block">{probe.key === 'backend' ? '后端 API' : 'ComfyUI'}</span>
                  <code className="break-all" style={{ color: 'var(--muted)' }}>{probe.url}</code>
                </div>
                <div className="flex items-center gap-2">
                  <span style={{ color: probe.http_status === 200 ? 'var(--ok, #22c55e)' : 'var(--warn, #f59e0b)' }}>
                    {probe.http_status == null ? '探测失败' : `HTTP ${probe.http_status}`}
                  </span>
                  <button type="button" className="underline" aria-label={`复制 ${probe.key === 'backend' ? '后端 API' : 'ComfyUI'} 的 Tailscale 地址`}
                    onClick={() => void copyAddress(probe.url)}>复制地址</button>
                </div>
              </div>
            ))}
          </div>
          {copyMessage && <p role="status" className="mt-1 text-xs">{copyMessage}</p>}
          {sidecar && (
            <details className="mt-3 border-t pt-2 text-xs" style={{ borderColor: 'var(--border)' }}>
              <summary className="cursor-pointer">ComfyUI 监听地址（{sidecar.listens.length}）</summary>
              <ul className="mt-2 space-y-1">
                {sidecar.listens.map((address) => <li key={address}><code>{address}</code></li>)}
                {sidecar.missing_listens.map((address) => <li key={address} style={{ color: 'var(--warn, #f59e0b)' }}>缺少监听 <code>{address}</code></li>)}
              </ul>
            </details>
          )}
        </Panel>
      </div>
      <Panel icon={<Activity size={14} />} title="ComfyUI 运行">
        <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs">
          <span>队列任务 {bridgeChecked && bridge.online ? bridge.queue_depth : '—'}</span>
          {render && <span>桥接任务 {render.task_id} · 已占用 {render.held_seconds.toFixed(1)} 秒</span>}
          <span>最近启动 {startedAt == null ? '—' : new Date(startedAt * 1000).toLocaleString()}</span>
          <span title="systemd 为此单元记录的自动重启次数">自动重启 {restartCount == null ? '—' : `${restartCount} 次`}</span>
        </div>
      </Panel>
    </section>
  )
}


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
    <div className="console-card"
      style={{
        padding: '16px 18px',
      }}
    >
      <div style={{ fontSize: 13, color: 'var(--muted)', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 28,
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
          style={{ fontSize: 13, color: subColor ?? 'var(--muted)', marginTop: 4 }}
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
    <div className="console-card"
      style={{
        padding: '16px 18px',
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
        <h3 style={{ fontSize: 15, color: 'var(--text)', fontWeight: 500, flex: 1 }}>
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

function SystemStatus() {
  const comfyHealth = useComfyHealth()
  const cache = useComfyCacheClear(comfyHealth)
  const { data: gpuData } = useSysGpus()
  const { data: sysStats } = useSysStats()
  const { data: runners } = useRunners()
  const { data: groupData } = useGpuGroups()
  const killProcess = useKillProcess()
  const cards = layoutGpuCards(gpuData?.gpus ?? [], groupData?.groups ?? [])
  const groupCount = cards.filter((card) => card.kind === 'group').length
  const gpuLabel = gpuData
    ? `${gpuData.gpus.length}× GPU${groupCount ? `（${groupCount} 组）` : ''}`
    : 'GPU'

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

  const gpuCacheControls = (gpus: SysGpuInfo[]) => {
    const target = `gpu-${gpus.map((gpu) => gpu.index).join('+')}`
    const hostsComfy = gpus.some((gpu) =>
      gpu.processes?.some((proc) => proc.managed && proc.model_name === 'ComfyUI'),
    )
    if (!hostsComfy && cache.target !== target) return undefined
    return <ComfyCacheControls cache={cache} target={target} canRequest={hostsComfy} />
  }

  return (
    <>
      <section aria-labelledby="dashboard-resources">
        <h2 id="dashboard-resources" className="mb-3 text-base font-semibold">资源与运行时</h2>
        {/* mini system stats */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
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
            label="物理内存"
            value={sysStats ? `${fmt(sysStats.memory_used_gb)}G` : '—'}
            hint={sysStats ? `可用 ${fmt(sysStats.memory_available_gb)} / 总计 ${fmt(sysStats.memory_total_gb)} GiB` : ''}
          >
            <ComfyCacheControls cache={cache} target="memory">
              <p style={{ color: 'var(--muted)' }}>
                ComfyUI 主进程内存 {cache.rss == null ? '—' : `${(cache.rss / 1024 ** 3).toFixed(1)} GiB`}
              </p>
            </ComfyCacheControls>
          </MiniStat>
          <MiniStat
            label="交换空间"
            value={sysStats ? `${fmt(sysStats.swap_used_gb)}G` : '—'}
            hint={sysStats ? `/ ${fmt(sysStats.swap_total_gb)}G` : ''}
          />
          <MiniStat
            label="磁盘"
            value={sysStats ? `${fmt(sysStats.disk_used_gb)}G` : '—'}
            hint={sysStats ? `/ ${fmt(sysStats.disk_total_gb)}G` : ''}
          />
        </div>

        <Panel icon={<Cpu size={14} />} title={gpuLabel}>
          {/* GPU panels */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 340px), 1fr))',
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
                  cacheControls={gpuCacheControls(card.gpus)}
                />
              ) : (
                <GpuCard
                  key={card.gpu.index}
                  gpu={card.gpu}
                  runner={runnerOf([card.gpu.index])}
                  onKill={onKill}
                  cacheControls={gpuCacheControls([card.gpu])}
                />
              ),
            )}
            {!gpuData && (
              <div style={{ fontSize: 11, color: 'var(--muted)', padding: 16 }}>
                等待 GPU 数据…
              </div>
            )}
          </div>
        </Panel>
        <VLLMPanel />
        <RuntimePanel />
      </section>
      <SystemConnections health={comfyHealth} />
    </>
  )
}

function SystemProcesses() {
  const query = useSysProcesses()
  const procData = query.data
  const [search, setSearch] = useState('')
  const [procSort, setProcSort] = useState<{ key: ProcSortKey; dir: ProcSortDir }>({ key: 'cpu', dir: 'desc' })
  const toggleProcSort = (key: ProcSortKey) =>
    setProcSort((s) => (s.key === key ? { key, dir: s.dir === 'desc' ? 'asc' : 'desc' } : { key, dir: 'desc' }))
  const term = search.trim().toLowerCase()
  const visibleProcesses = sortProcesses(procData?.processes ?? [], procSort.key, procSort.dir)
    .filter((row) => [String(row.pid), row.name, row.command].some((field) => field.toLowerCase().includes(term)))
  const netProbeOk = procData?.net_probe?.available ?? false
  const sortMark = (key: ProcSortKey) => (procSort.key === key ? (procSort.dir === 'desc' ? ' ▼' : ' ▲') : '')

  return (
    <Panel icon={<Cpu size={14} />} title="系统进程">
      <input
        type="search"
        aria-label="搜索进程"
        placeholder="搜索 PID、名称或命令"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        className="mb-2 w-full max-w-xs rounded border px-2 py-1 text-sm"
        style={{ borderColor: 'var(--border)' }}
      />
      {query.isError && <p role="alert" className="text-sm">进程数据加载失败</p>}
      {query.isLoading && <p role="status" className="text-sm">正在加载进程…</p>}
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
        进程 ({visibleProcesses.length})
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
                {([
                  ['cpu', 'CPU%'],
                  ['mem', '内存'],
                  ['net', '网络'],
                ] as const).map(([key, label]) => (
                  <th key={key} scope="col" className="px-2 py-1"
                    aria-sort={procSort.key === key ? (procSort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                    <button type="button" onClick={() => toggleProcSort(key)}
                      title={key === 'net' && !netProbeOk ? '网络采集数据不可用' : undefined}>
                      {label}{sortMark(key)}
                    </button>
                  </th>
                ))}
                <th style={{ padding: '4px 8px' }}>NAME</th>
                <th style={{ padding: '4px 8px' }}>COMMAND</th>
              </tr>
            </thead>
            <tbody>
              {visibleProcesses.map((p) => (
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
          暂无进程数据
        </div>
      )}
    </Panel>
  )
}

function useComfyCacheClear(health: ReturnType<typeof useComfyHealth>) {
  const clear = useFreeComfyCache()
  const [confirming, setConfirming] = useState(false)
  const [target, setTarget] = useState<string>()
  const comfy = health.data
  const busy = !!comfy?.running_render || (comfy?.queue_depth ?? 0) > 0
  const ready = !health.isError && comfy?.online === true && comfy.queue_depth === 0 && !busy
  let unavailable = '正在读取 ComfyUI 队列'
  if (health.isError) unavailable = '暂时无法确认队列状态'
  else if (comfy?.online === false) unavailable = 'ComfyUI 离线'
  else if (busy) unavailable = '有任务正在执行或排队'

  const requestClear = async (actionTarget: string) => {
    if (!ready || confirming || clear.isPending) return
    setConfirming(true)
    const confirmed = await confirmDialog({
      title: actionTarget === 'memory' ? '清理 ComfyUI 缓存' : '释放 ComfyUI 显存',
      message: '卸载 ComfyUI 缓存模型并清理节点执行缓存。下次生成可能需要重新加载模型。',
      confirmText: '清理缓存',
    })
    setConfirming(false)
    if (confirmed) {
      setTarget(actionTarget)
      clear.mutate()
    }
  }

  return { clear, confirming, target, ready, unavailable, requestClear, rss: comfy?.process_rss_bytes }
}

function ComfyCacheControls({ cache, target, canRequest = true, children }: {
  cache: ReturnType<typeof useComfyCacheClear>
  target: string
  canRequest?: boolean
  children?: ReactNode
}) {
  const { clear, ready, confirming, unavailable, requestClear } = cache
  const active = target === cache.target
  const isMemory = target === 'memory'
  const label = isMemory ? '清理 ComfyUI 缓存' : '释放 ComfyUI 显存'
  const freed = clear.data?.freed_bytes ?? 0

  return (
    <div className="mt-3 space-y-2 border-t pt-2 text-xs" style={{ borderColor: 'var(--border)' }}>
      {children}
      {canRequest && (
        <button type="button"
          className="rounded border px-2 py-1 disabled:cursor-not-allowed disabled:opacity-50"
          style={{ borderColor: 'var(--border-strong)', color: 'var(--accent)' }}
          disabled={!ready || confirming || clear.isPending}
          onClick={() => void requestClear(target)}>
          {active && clear.isPending ? '正在请求清理…' : label}
        </button>
      )}
      {canRequest && !ready && <p style={{ color: 'var(--muted)' }}>{unavailable}</p>}
      {active && clear.isSuccess && (
        <p role="status">{isMemory
          ? '已提交清理请求，内存读数会继续刷新。'
          : freed > 0
            ? `已观察到 ComfyUI 归还 ${(freed / 1024 ** 3).toFixed(1)} GiB 显存。`
            : '已提交清理请求，暂未观察到显存下降。'}</p>
      )}
      {active && clear.isError && <p role="alert" style={{ color: 'var(--error)' }}>{clear.error.message}</p>}
    </div>
  )
}

function MiniStat({
  label,
  value,
  hint,
  children,
}: {
  label: string
  value: string
  hint?: string
  children?: ReactNode
}) {
  return (
    <div className="console-card px-4 py-3">
      <div style={{ fontSize: 12, color: 'var(--muted)', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 24,
          fontWeight: 650,
          color: 'var(--text)',
          fontVariantNumeric: 'tabular-nums',
          marginTop: 2,
        }}
      >
        {value}
        {hint && (
          <span style={{ color: 'var(--muted)', fontSize: 12, display: 'block', fontWeight: 400 }}>
            {hint}
          </span>
        )}
      </div>
      {children}
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
  cacheControls,
}: {
  group: GpuGroup
  gpus: SysGpuInfo[]
  runner: RunnerInfo | null
  onKill: (pid: number, mem: number) => void
  cacheControls?: ReactNode
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
        {cacheControls}
        {procs.length > 0 && (
          <details className="console-gpu-processes">
            <summary>GPU 进程（{procs.length}）</summary>
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
          </details>
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
  cacheControls,
}: {
  gpu: SysGpuInfo
  runner: RunnerInfo | null
  onKill: (pid: number, mem: number) => void
  cacheControls?: ReactNode
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
      {cacheControls}
      {gpu.processes && gpu.processes.length > 0 && (
        <details className="console-gpu-processes">
          <summary>GPU 进程（{gpu.processes.length}）</summary>
          {gpu.processes.map((proc) => (
            <ProcRow key={proc.pid} proc={proc} onKill={onKill} />
          ))}
        </details>
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

function VLLMPanel() {
  const { data, isLoading, error } = useVLLMMetrics()
  const instances = data?.instances ?? []

  return (
    <Panel icon={<Cpu size={16} color="var(--accent)" />} title="vLLM KV Cache · 调度"
      rightSlot={instances.length > 0 && <span className="text-xs" style={{ color: 'var(--muted)' }}>{instances.length} 个实例 · 每 3 秒刷新</span>}
    >
      {isLoading && <p role="status" className="text-sm">正在加载 vLLM 指标…</p>}
      {error && <p role="alert" className="text-sm">vLLM 指标接口不可达</p>}
      {!isLoading && !error && instances.length === 0 && <p className="text-sm">没有正在运行的 vLLM 实例</p>}
      {instances.length > 0 && (
        <div className="console-table-wrap">
          <table className="console-table" aria-label="vLLM 实例指标">
            <thead>
              <tr>
                {['实例', 'KV 使用', '总容量', '运行中', '排队', 'Prefix 命中'].map((label) => <th scope="col" key={label}>{label}</th>)}
              </tr>
            </thead>
            <tbody>
              {instances.map((instance) => <VLLMRow key={`${instance.name}-${instance.port}`} instance={instance} />)}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function VLLMRow({ instance }: { instance: VLLMInstance }) {
  const { name, port, healthy, error, config, stats } = instance
  const usage = stats.kv_cache_usage_perc
  const usagePct = usage == null ? null : Math.round(usage * 100)
  const blocks = Number(config.num_gpu_blocks)
  const blockSize = Number(config.block_size)
  const total = Number.isFinite(blocks * blockSize) && blocks > 0 && blockSize > 0 ? blocks * blockSize : null
  const prefixOff = config.enable_prefix_caching?.toLowerCase() === 'false'
  const queries = stats.prefix_cache_queries_total
  const hits = stats.prefix_cache_hits_total
  const hitRate = stats.prefix_cache_hit_rate
  let prefix = '—'
  if (prefixOff) prefix = '关闭'
  else if (queries === 0) prefix = '暂无查询'
  else if (hitRate != null) prefix = `${Math.round(hitRate * 100)}%`

  return (
    <tr>
      <th scope="row">
        <span className="break-all">{name}</span>
        <span className="metric-detail">端口 {port}</span>
      </th>
      {!healthy ? (
        <td colSpan={5} style={{ color: 'var(--error)' }}>{error || '实例指标不可用'}</td>
      ) : (
        <>
          <td>
            <span className="metric-value">{usagePct == null ? '—' : `${usagePct}%`}</span>
            {usagePct != null && (
              <progress className="console-kv-meter" aria-label={`${name} KV 使用率`} max={100} value={usagePct}
                style={{ accentColor: usagePct > 85 ? 'var(--error)' : undefined }} />
            )}
          </td>
          <td>
            <span className="metric-value" title={total == null ? undefined : `${total.toLocaleString()} tokens`}>
              {total == null ? '—' : `${fmtN(total)} tokens`}
            </span>
            {total != null && <span className="metric-detail">{blocks} blocks × {blockSize}</span>}
          </td>
          <td className="metric-value">{stats.running ?? '—'}</td>
          <td className="metric-value">{stats.waiting ?? '—'}</td>
          <td>
            <span className="metric-value">{prefix}</span>
            {!prefixOff && hits != null && queries != null && <span className="metric-detail">{hits} / {queries}</span>}
          </td>
        </>
      )}
    </tr>
  )
}
