import { useId, useState } from 'react'
import { Activity, ChevronDown, X } from 'lucide-react'
import { useGpuStats, useSystemStats, type GpuInfo } from '../../api/gpuStats'
import './TopbarGpuMonitor.css'

type MetricColor = 'cpu' | 'ram' | 'gpu' | 'vram'

const HISTORY_MS = 60_000

interface Sample {
  time: number
  vram: Record<number, number | null>
}

function percent(value: number, total = 100) {
  return total > 0 ? Math.round(Math.max(0, Math.min(100, 100 * value / total))) : null
}

function memory(value: number) {
  return value.toFixed(1) + ' GiB'
}

function gpuLabel(gpu: GpuInfo) {
  const model = gpu.name.match(/(?:RTX\s+)?PRO\s+\d+|RTX\s+\d+\s*(?:Ti|SUPER)?/i)
  return model?.[0].trim().replace(/^RTX PRO/i, 'PRO') || `GPU ${gpu.index}`
}

function Meter({ color, value }: { color: MetricColor; value: number | null }) {
  return (
    <span aria-hidden="true" className="resource-monitor-meter" data-color={color}>
      <span style={{ width: `${value ?? 0}%` }} />
    </span>
  )
}

function Reading({ label, value, percent, color }: {
  label: string
  value: string
  percent: number | null
  color: MetricColor
}) {
  return (
    <div className="resource-monitor-reading">
      <span>{label}</span>
      <strong>{value}</strong>
      <Meter color={color} value={percent} />
    </div>
  )
}

function displayPercent(value: number | null) {
  return value === null ? '—' : `${value}%`
}

function GpuDetails({ gpu, samples }: { gpu: GpuInfo; samples: Sample[] }) {
  const vram = percent(gpu.memory_used_mb, gpu.memory_total_mb)
  const load = percent(gpu.utilization_gpu)
  const latest = samples.at(-1)
  const elapsed = latest && latest.time - samples[0].time
  const processes = [...(gpu.processes ?? [])]
    .sort((left, right) => right.used_gpu_memory_mb - left.used_gpu_memory_mb)

  return (
    <section className="resource-monitor-card">
      <h3><small>GPU {gpu.index}</small> {gpu.name}</h3>
      <Reading color="gpu" label="GPU 负载" value={displayPercent(load)} percent={load} />
      <Reading color="vram" label="显存"
        value={vram === null ? '—' : `${memory(gpu.memory_used_mb / 1024)} / ${memory(gpu.memory_total_mb / 1024)}`}
        percent={vram}
      />
      <div className="resource-monitor-meta">
        {vram !== null && <span>空闲 {memory((gpu.memory_free_mb ?? gpu.memory_total_mb - gpu.memory_used_mb) / 1024)}</span>}
        <span>温度 {gpu.temperature ? `${gpu.temperature} °C` : '—'}</span>
        <span>功耗 {gpu.power_draw_w ? `${Math.round(gpu.power_draw_w)}${gpu.power_limit_w ? ` / ${Math.round(gpu.power_limit_w)}` : ''} W` : '—'}</span>
      </div>
      {latest && elapsed !== undefined && elapsed > 0 && vram !== null && (
        <div className="resource-monitor-history">
          <span>近 {Math.ceil(elapsed / 1000)} 秒显存占用</span>
          <div className="resource-monitor-chart" role="img" aria-label={`显存占用趋势，当前 ${displayPercent(vram)}`}>
            {samples.map(({ time, vram: values }) => {
              const value = values[gpu.index]
              return value != null && (
                <span key={time} title={displayPercent(value)}
                  style={{ left: `${100 * (time - latest.time) / HISTORY_MS + 100}%`, height: `${value}%` }}
                />
              )
            })}
          </div>
        </div>
      )}
      {processes.length > 0 && (
        <details className="resource-monitor-processes">
          <summary>显存进程（{processes.length}）</summary>
          <ul>
            {processes.map((process) => (
              <li key={process.pid}>
                <span title={process.model_name || process.name}>{process.model_name || process.name || '进程'}</span>
                <small>PID {process.pid}</small>
                <strong>{memory(process.used_gpu_memory_mb / 1024)}</strong>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

export default function TopbarGpuMonitor() {
  const { data: gpus = [], dataUpdatedAt } = useGpuStats()
  const { data: system } = useSystemStats()
  const panelId = useId()
  const [samples, setSamples] = useState<Sample[]>([])
  if (gpus.length && dataUpdatedAt && dataUpdatedAt !== samples.at(-1)?.time) {
    setSamples([
      ...samples.filter(({ time }) => time > dataUpdatedAt - HISTORY_MS),
      {
        time: dataUpdatedAt,
        vram: Object.fromEntries(gpus.map((gpu) => [gpu.index, percent(gpu.memory_used_mb, gpu.memory_total_mb)])),
      },
    ])
  }
  if (!gpus.length && !system) return null
  const ram = system ? percent(system.memory_used_gb, system.memory_total_gb) : null
  const cpu = system ? percent(system.cpu_usage_percent) : null

  return (
    <div className="resource-monitor">
      <button type="button" className="resource-monitor-trigger" popoverTarget={panelId}
        aria-label="资源监控" title="展开资源监控">
        <Activity size={15} />
        <span className="resource-monitor-compact-label">资源</span>
        {system && (
          <span className="resource-monitor-chip">
            <span>CPU <b data-color="cpu">{displayPercent(cpu)}</b></span>
            <span>内存 <b data-color="ram">{displayPercent(ram)}</b></span>
            <Meter color="ram" value={ram} />
          </span>
        )}
        {gpus.slice(0, 2).map((gpu) => {
          const vram = percent(gpu.memory_used_mb, gpu.memory_total_mb)
          return (
            <span className="resource-monitor-chip" key={gpu.index} title={gpu.name}>
              <span>{gpuLabel(gpu)} <small>· {gpu.index}</small></span>
              <b data-color="gpu" title="GPU 负载">{displayPercent(percent(gpu.utilization_gpu))}</b>
              <span>显存 <b data-color="vram">{displayPercent(vram)}</b></span>
              <Meter color="vram" value={vram} />
            </span>
          )
        })}
        {gpus.length > 2 && <small className="resource-monitor-extra">+{gpus.length - 2}</small>}
        <ChevronDown size={13} />
      </button>

      <section popover="auto" id={panelId} className="resource-monitor-panel" aria-labelledby={`${panelId}-title`}>
        <header>
          <h2 id={`${panelId}-title`}>资源监控</h2>
          <button type="button" popoverTarget={panelId} popoverTargetAction="hide" aria-label="关闭资源监控">
            <X size={16} />
          </button>
        </header>
        {system && (
          <section className="resource-monitor-card">
            <h3>主机</h3>
            <Reading color="cpu" label="CPU 负载" value={displayPercent(cpu)} percent={cpu} />
            <Reading color="ram" label="物理内存"
              value={`${memory(system.memory_used_gb)} / ${memory(system.memory_total_gb)}`}
              percent={ram}
            />
            <div className="resource-monitor-meta">
              <span>{system.cpu_count} 逻辑核</span>
              <span>可用 {memory(system.memory_available_gb)}</span>
            </div>
          </section>
        )}
        {gpus.map((gpu) => <GpuDetails key={gpu.index} gpu={gpu} samples={samples} />)}
      </section>
    </div>
  )
}
