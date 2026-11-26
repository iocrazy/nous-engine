import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { confirmDialog } from '../stores/confirm'
import { apiFetch } from '../api/client'
import GalleryImagePicker from '../components/playground/GalleryImagePicker'
import './TrainingStudio.css'

const ROOT = '/api/v1/training'
const TABS = ['数据集', '模型与目录', '发起训练', '训练记录', '效果预览'] as const
const RUN_STATUS: Record<string, string> = {
  queued: '排队中', preparing: '准备数据', running: '训练中', completed: '已完成',
  failed: '失败', interrupted: '已中断', cancelling: '正在取消', cancelled: '已取消',
}
const ACTIVE = ['queued', 'preparing', 'running', 'cancelling']
const DOWNLOAD_STATUS: Record<string, string> = {
  queued: '排队中', downloading: '下载中', verifying: '校验中', cancelling: '正在取消',
  completed: '已下载', cancelled: '已取消', failed: '下载失败',
}
const ACTIVE_DOWNLOAD = ['queued', 'downloading', 'verifying', 'cancelling']
const RETRYABLE = ['failed', 'interrupted', 'cancelled']

type Dataset = { id: string; name: string; count: number }
type Sample = { id: string; name: string; caption: string; width: number; height: number }
type Run = {
  id: string; name: string; status: string; total_steps: number; error: string | null;
  started_at: string | null; finished_at: string | null; kind: 'training' | 'preview';
  checkpoint: string | null; prompt: string | null; seed: number | null; published: string | null;
}
type RunDetail = Run & { step: number; log: string; artifacts: string[]; loss: { step: number; loss: number }[] }
type Download = {
  id: string | null; state: string | null; source: string | null; error: string | null;
  bytes: number; total_bytes: number; available_bytes: number | null; target: string | null;
}
type Environment = {
  can_queue: boolean; recipe: string; revision: string; model_path: string | null; missing: string[];
  trainer_path: string | null; trainer_error: string | null; gpu_error: string | null;
  storage_error: string | null; free_bytes: number | null;
  gpu: { name: string; free_mib: number; total_mib: number } | null;
}

const DEFAULTS = { epochs: 3, repeat: 1, rank: 32, learning_rate: 0.0001, max_pixels: 1048576, save_steps: 0 }
const SETTINGS: { key: keyof typeof DEFAULTS; label: string; min: number; max: number; step: number }[] = [
  { key: 'epochs', label: '训练轮数', min: 1, max: 100, step: 1 },
  { key: 'repeat', label: '数据重复次数', min: 1, max: 100, step: 1 },
  { key: 'rank', label: 'LoRA Rank', min: 1, max: 128, step: 1 },
  { key: 'learning_rate', label: '学习率', min: 0.00000001, max: 0.01, step: 0.00000001 },
  { key: 'max_pixels', label: '最大像素数', min: 65536, max: 4194304, step: 1 },
  { key: 'save_steps', label: '每几步保存（0 为每轮）', min: 0, max: 10000, step: 1 },
]

function request<T>(path: string, method: string, body?: unknown) {
  return apiFetch<T>(ROOT + path, {
    method, body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body),
  })
}

async function captionSidecar(file: File | undefined) {
  if (!file) return ''
  if (file.size > 32 * 1024) throw new Error(`${file.name} 超过 TXT 大小限制`)
  return (await file.text()).trim()
}

export default function TrainingStudio() {
  const client = useQueryClient()
  const [tab, setTab] = useState<typeof TABS[number]>('数据集')
  const [datasetId, setDatasetId] = useState('')
  const [search, setSearch] = useState('')
  const [captionFilter, setCaptionFilter] = useState('all')
  const [sampleId, setSampleId] = useState('')
  const [caption, setCaption] = useState('')
  const [captionPrompt, setCaptionPrompt] = useState('描述画面主体、环境、构图和光线，输出适用于图片训练的简洁描述。')
  const [replaceCaptions, setReplaceCaptions] = useState(false)
  const [captionFailures, setCaptionFailures] = useState<string[]>([])
  const [picked, setPicked] = useState<string[]>([])
  const [downloadSource, setDownloadSource] = useState('modelscope')
  const [datasetName, setDatasetName] = useState('')
  const [checkpoint, setCheckpoint] = useState('')
  const [previewPrompt, setPreviewPrompt] = useState('')
  const [seed, setSeed] = useState(42)
  const [divider, setDivider] = useState(50)
  const [filename, setFilename] = useState('')
  const [jobName, setJobName] = useState('')
  const [jobId, setJobId] = useState('')
  const [params, setParams] = useState(DEFAULTS)
  const [allowBlank, setAllowBlank] = useState(false)
  const [picker, setPicker] = useState(false)
  const [status, setStatus] = useState('')

  const datasets = useQuery({
    queryKey: ['training', 'datasets'], queryFn: () => apiFetch<Dataset[]>(ROOT + '/datasets'),
  })
  const dataset = useQuery({
    queryKey: ['training', 'dataset', datasetId], enabled: Boolean(datasetId),
    queryFn: () => apiFetch<Dataset & { samples: Sample[] }>(ROOT + '/datasets/' + datasetId),
  })
  const captionModel = useQuery({
    queryKey: ['training', 'caption-model'], enabled: tab === '数据集',
    queryFn: () => apiFetch<{ model: string | null }>(ROOT + '/caption-model'),
  })
  const check = useQuery({
    queryKey: ['training', 'preflight'], enabled: tab === '模型与目录' || tab === '发起训练',
    queryFn: ({ signal }) => apiFetch<Environment>(ROOT + '/preflight', { signal }),
    staleTime: 30_000, refetchOnWindowFocus: false, retry: false,
  })
  const download = useQuery({
    queryKey: ['training', 'download'], enabled: tab === '模型与目录',
    queryFn: ({ signal }) => apiFetch<Download>(ROOT + '/model-download', { signal }),
    refetchInterval: tab === '模型与目录' ? 2500 : false,
  })
  const downloadDone = download.data?.state === 'completed'
  useEffect(() => {
    if (downloadDone) void client.invalidateQueries({ queryKey: ['training', 'preflight'] })
  }, [client, downloadDone])

  const downloadActive = ACTIVE_DOWNLOAD.includes(download.data?.state ?? '')
  const modelDownload = useMutation({
    mutationFn: (action: 'start' | 'cancel') =>
      action === 'start'
        ? request<Download>('/model-download', 'POST', { source: downloadSource })
        : request<Download>(`/model-download/${download.data?.id}/cancel`, 'POST'),
    onSettled: () => client.invalidateQueries({ queryKey: ['training', 'download'] }),
  })
  const viewingJobs = tab === '训练记录' || tab === '效果预览'
  const jobs = useQuery({
    queryKey: ['training', 'runs'], enabled: viewingJobs,
    queryFn: ({ signal }) => apiFetch<Run[]>(ROOT + '/runs', { signal }), refetchInterval: viewingJobs ? 4000 : false,
  })
  const job = useQuery({
    queryKey: ['training', 'run', jobId], enabled: viewingJobs && Boolean(jobId),
    queryFn: ({ signal }) => apiFetch<RunDetail>(ROOT + '/runs/' + jobId, { signal }),
    refetchInterval: viewingJobs ? (query) => ACTIVE.includes(query.state.data?.status ?? 'queued') ? 2000 : false : false,
  })

  const refreshDataset = (id: string) => Promise.all([
    client.invalidateQueries({ queryKey: ['training', 'datasets'] }),
    client.invalidateQueries({ queryKey: ['training', 'dataset', id] }),
  ])

  const changeDataset = (id: string) => {
    setDatasetId(id)
    setPicked([])
    setCaptionFailures([])
    setSampleId('')
    setCaption('')
  }

  const makeDataset = useMutation({
    mutationFn: () => request<Dataset>('/datasets', 'POST', { name: datasetName }),
    onSuccess: async (result) => {
      changeDataset(result.id)
      setDatasetName('')
      await refreshDataset(result.id)
    },
  })

  const upload = useMutation({
    mutationFn: async ({ id, files, origin }: { id: string; files: File[]; origin: 'gallery' | 'upload' }) => {
      const txt = new Map(files.filter(f => /\.txt$/i.test(f.name)).map(f => [f.name.slice(0, -4), f]))
      const images = files.filter(f => !/\.txt$/i.test(f.name))
      if (images.length === 0) throw new Error('请选择图片，或同时选择图片和同名 TXT')
      const used = new Set<string>()
      const errors: string[] = []
      let completed = 0
      for (const image of images) {
        try {
          const stem = image.name.replace(/\.[^.]+$/, '')
          const form = new FormData()
          form.set('file', image)
          form.set('caption', await captionSidecar(txt.get(stem)))
          form.set('origin', origin)
          await request(`/datasets/${id}/samples`, 'POST', form)
          completed++
          used.add(stem)
        } catch (err) {
          errors.push(`${image.name} · ${(err as Error).message}`)
        }
      }
      const unmatched = [...txt.keys()].filter(name => !used.has(name)).map(name => `${name}.txt`)
      return [
        `已添加 ${completed} 张图片`,
        ...(unmatched.length ? [`未配对 TXT · ${unmatched.join('、')}`] : []),
        ...errors,
      ].join('\n')
    },
    onSuccess: setStatus,
    onSettled: async (_result, _error, variable) => refreshDataset(variable.id),
  })

  const saveCaption = useMutation({
    mutationFn: () => request(`/datasets/${datasetId}/samples/${sampleId}`, 'PATCH', { caption }),
    onSuccess: () => refreshDataset(datasetId),
  })

  const captionBatch = useMutation({
    mutationFn: async ({ id, ids }: { id: string; ids: string[] }) => {
      const failed: string[] = []
      let done = 0
      let skipped = 0
      for (const sample of ids) {
        try {
          const response = await request<{ state: 'saved' | 'skipped' }>(
            `/datasets/${id}/samples/${sample}/caption`, 'POST',
            { instruction: captionPrompt, overwrite: replaceCaptions },
          )
          if (response.state === 'saved') done++
          else skipped++
        } catch {
          failed.push(sample)
        }
        setStatus(`已标注 ${done} · 跳过 ${skipped} · 失败 ${failed.length} / ${ids.length} 张`)
      }
      return failed
    },
    onSuccess: setCaptionFailures,
    onSettled: async (_result, _error, variables) => refreshDataset(variables.id),
  })

  async function requestCaptions(ids: string[]) {
    if (replaceCaptions && !await confirmDialog({
      message: `重新生成所选 ${ids.length} 张素材的描述？`, confirmText: '覆盖描述',
    })) return
    captionBatch.mutate({ id: datasetId, ids })
  }

  const submit = useMutation({
    mutationFn: () => request<Run>('/runs', 'POST', {
      ...params, name: jobName, dataset_id: datasetId, sample_ids: picked, allow_blank: allowBlank,
    }),
    onSuccess: async (result) => {
      setJobId(result.id)
      setTab('训练记录')
      await client.invalidateQueries({ queryKey: ['training', 'runs'] })
    },
  })

  const manageRun = useMutation({
    mutationFn: (action: 'cancel' | 'retry') => request<Run>(`/runs/${jobId}/${action}`, 'POST'),
    onSuccess: async (result) => {
      setJobId(result.id)
      await Promise.all([
        client.invalidateQueries({ queryKey: ['training', 'runs'] }),
        client.invalidateQueries({ queryKey: ['training', 'run', result.id] }),
      ])
    },
  })

  const createPreview = useMutation({
    mutationFn: (selected: string) => request<Run>(`/runs/${jobId}/preview`, 'POST', {
      checkpoint: selected, prompt: previewPrompt, seed,
    }),
    onSuccess: async (result) => {
      setJobId(result.id)
      await client.invalidateQueries({ queryKey: ['training', 'runs'] })
    },
  })

  const publish = useMutation({
    mutationFn: () => request<{ path: string; comfy_listed: boolean }>(`/runs/${jobId}/publish`, 'POST', { filename }),
    onSuccess: async (result) => {
      setStatus(result.comfy_listed
        ? `已发布到 ${result.path}，ComfyUI 已列出该 LoRA。`
        : `已写入 ${result.path}，可在 Playground 同步 LoRA 目录。`)
      await Promise.all([
        client.invalidateQueries({ queryKey: ['training', 'run', jobId] }),
        client.invalidateQueries({ queryKey: ['comfy', 'loras'] }),
      ])
    },
  })

  const samples = dataset.data?.samples ?? []
  const visibleSamples = samples.filter(sample => {
    const matches = sample.name.toLowerCase().includes(search.trim().toLowerCase())
    return matches && (captionFilter === 'all' || Boolean(sample.caption) === (captionFilter === 'labeled'))
  })
  const samplesById = new Map(samples.map(sample => [sample.id, sample]))
  const focused = samples.find(item => item.id === sampleId)
  const run = job.data
  const selectedCheckpoint = run?.artifacts.includes(checkpoint) ? checkpoint : run?.artifacts[0]
  const error = datasets.error || dataset.error || check.error || jobs.error || job.error ||
    makeDataset.error || upload.error || saveCaption.error || submit.error || manageRun.error ||
    download.error || modelDownload.error || createPreview.error || publish.error

  function toggleSample(id: string) {
    setPicked(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])
  }

  return (
    <main className="training-studio console-page console-controls">
      <h1 className="console-title">训练工坊</h1>
      <nav className="training-tabs" aria-label="训练工坊导航">
        {TABS.map(item => <button type="button" key={item} aria-current={tab === item ? 'page' : undefined}
          onClick={() => { setStatus(''); setTab(item) }}>{item}</button>)}
      </nav>
      {error && <p role="alert" className="training-error">{error.message}</p>}
      {status && <p role="status" className="training-message">{status}</p>}

      {tab === '数据集' && (
        <>
          <div className="training-toolbar">
            <label>数据集
              <select value={datasetId} disabled={captionBatch.isPending} onChange={e => changeDataset(e.target.value)}>
                <option value="">选择数据集</option>
                {datasets.data?.map(item => <option key={item.id} value={item.id}>{item.name} · {item.count} 张</option>)}
              </select>
            </label>
            <form onSubmit={e => { e.preventDefault(); makeDataset.mutate() }}>
              <label>新建数据集<input value={datasetName} maxLength={120} onChange={e => setDatasetName(e.target.value)} /></label>
              <button disabled={!datasetName.trim() || makeDataset.isPending || captionBatch.isPending}>创建</button>
            </form>
          </div>
          {datasetId && (
            <>
              <div className="training-toolbar">
                <label>导入图片和同名 TXT
                  <input type="file" accept=".jpg,.jpeg,.png,.webp,.txt" multiple disabled={upload.isPending}
                    onChange={e => {
                      const files = [...(e.target.files ?? [])]
                      if (files.length) upload.mutate({ id: datasetId, files, origin: 'upload' })
                      e.target.value = ''
                    }} />
                </label>
                <button disabled={upload.isPending} onClick={() => setPicker(true)}>从图库添加</button>
                <label>搜索素材<input type="search" value={search} onChange={e => setSearch(e.target.value)} /></label>
                <label>标注状态<select value={captionFilter} onChange={e => setCaptionFilter(e.target.value)}>
                  <option value="all">全部</option>
                  <option value="labeled">已标注</option>
                  <option value="empty">待标注</option>
                </select></label>
                <button disabled={!visibleSamples.length} onClick={() =>
                  setPicked(current => [...new Set([...current, ...visibleSamples.map(sample => sample.id)])])
                }>全选当前结果</button>
                <button disabled={!picked.length} onClick={() => setPicked([])}>清空选择</button>
                <button className="training-primary" disabled={!picked.length} onClick={() => setTab('发起训练')}>
                  训练所选素材 · {picked.length} 张
                </button>
              </div>
              {captionModel.data?.model && (
                <details className="training-panel">
                  <summary>AI 批量标注 · {captionModel.data.model}</summary>
                  <label>描述指令<textarea value={captionPrompt} maxLength={1200} disabled={captionBatch.isPending}
                    onChange={e => setCaptionPrompt(e.target.value)} /></label>
                  <label className="training-check"><input type="checkbox" checked={replaceCaptions} disabled={captionBatch.isPending}
                    onChange={e => setReplaceCaptions(e.target.checked)} />覆盖已有描述</label>
                  <div className="training-toolbar">
                    <button disabled={!picked.length || !captionPrompt.trim() || captionBatch.isPending}
                      onClick={() => void requestCaptions(picked)}>标注所选素材</button>
                    {captionFailures.length > 0 && <button disabled={captionBatch.isPending || !captionPrompt.trim()}
                      onClick={() => void requestCaptions(captionFailures)}>重试失败的 {captionFailures.length} 张</button>}
                  </div>
                </details>
              )}
              {upload.isPending && <p role="status">正在导入素材…</p>}
              <div className="training-split">
                <div className="training-grid">
                  {visibleSamples.map(sample => (
                    <article key={sample.id} className="training-card">
                      <button type="button" onClick={() => { setSampleId(sample.id); setCaption(sample.caption) }}>
                        <img loading="lazy" src={`${ROOT}/datasets/${datasetId}/samples/${sample.id}`} alt={sample.name} />
                      </button>
                      <label><input type="checkbox" checked={picked.includes(sample.id)} onChange={() => toggleSample(sample.id)} />
                        {sample.name}</label>
                      <small>{sample.caption ? '已标注' : '待标注'}</small>
                    </article>
                  ))}
                </div>
                {focused && (
                  <aside className="training-panel">
                    <h2>{focused.name}</h2>
                    <p>{focused.width} × {focused.height}</p>
                    <label>训练描述<textarea rows={8} value={caption} maxLength={8000}
                      onChange={e => setCaption(e.target.value)} /></label>
                    <button disabled={saveCaption.isPending} onClick={() => saveCaption.mutate()}>保存描述</button>
                  </aside>
                )}
              </div>
            </>
          )}
          {picker && (
            <GalleryImagePicker destination={dataset.data?.name ?? '训练数据集'} onClose={() => setPicker(false)}
              onSelect={blob => {
                setPicker(false)
                upload.mutate({ id: datasetId, files: [new File([blob], 'gallery-image', { type: blob.type })], origin: 'gallery' })
              }} />
          )}
        </>
      )}

      {(tab === '模型与目录' || tab === '发起训练') && (
        <section className="training-panel">
          <div className="training-toolbar">
            <h2>Qwen-Image-2.1 · 文生图 LoRA</h2>
            <button onClick={() => void check.refetch()} disabled={check.isFetching}>
              {check.isFetching ? '正在检查…' : '检查训练环境'}
            </button>
          </div>
          {check.data && (
            <dl className="training-facts">
              <dt>模型目录</dt><dd>{check.data.model_path ?? '模型组件未就绪'}</dd>
              {check.data.missing.length > 0 && <><dt>缺少组件</dt><dd>{check.data.missing.join('、')}</dd></>}
              <dt>DiffSynth-Studio</dt><dd>{check.data.trainer_error ?? check.data.trainer_path}</dd>
              <dt>训练 GPU</dt><dd>{check.data.gpu?.name ?? '未配置'} {check.data.gpu &&
                `· 可用 ${(check.data.gpu.free_mib / 1024).toFixed(1)} GiB / ${(check.data.gpu.total_mib / 1024).toFixed(1)} GiB`}</dd>
              {check.data.gpu_error && <><dt>GPU 状态</dt><dd>{check.data.gpu_error}</dd></>}
              <dt>存储</dt><dd>{check.data.storage_error ?? `可用 ${((check.data.free_bytes ?? 0) / 1024 ** 3).toFixed(1)} GiB`}</dd>
            </dl>
          )}
        </section>
      )}

      {tab === '模型与目录' && download.data && (
        <section className="training-panel">
          <h2>模型下载</h2>
          <p>{download.data.target ?? '尚未配置模型根目录'}</p>
          <p>已准备 {(download.data.bytes / 1024 ** 3).toFixed(2)} / {(download.data.total_bytes / 1024 ** 3).toFixed(2)} GiB
            {download.data.available_bytes != null && ` · 磁盘剩余 ${(download.data.available_bytes / 1024 ** 3).toFixed(1)} GiB`}</p>
          <progress aria-label="模型下载进度" value={download.data.bytes} max={download.data.total_bytes} />
          {download.data.state && <p>{DOWNLOAD_STATUS[download.data.state] ?? download.data.state}</p>}
          {download.data.error && <p role="alert">{download.data.error}</p>}
          <div className="training-toolbar">
            <label>下载来源<select value={downloadSource} onChange={e => setDownloadSource(e.target.value)} disabled={downloadActive}>
              <option value="modelscope">ModelScope 魔搭</option>
              <option value="huggingface">Hugging Face</option>
            </select></label>
            {downloadActive
              ? <button disabled={modelDownload.isPending || download.data.state === 'cancelling'}
                  onClick={() => modelDownload.mutate('cancel')}>取消下载</button>
              : <button disabled={!download.data.target || Boolean(check.data?.model_path) || modelDownload.isPending}
                  onClick={() => modelDownload.mutate('start')}>下载或补齐组件</button>}
          </div>
        </section>
      )}

      {tab === '发起训练' && (
        <form className="training-panel" onSubmit={e => { e.preventDefault(); submit.mutate() }}>
          <p>{dataset.data?.name ?? '尚未选择数据集'} · {picked.length} 张已选</p>
          {picked.length > 0 && (
            <div className="training-selected" aria-label="已选训练素材">
              {picked.map(id => <img key={id} loading="lazy"
                src={`${ROOT}/datasets/${datasetId}/samples/${id}`} alt={samplesById.get(id)?.name ?? '训练素材'} />)}
            </div>
          )}
          <label>任务名称<input value={jobName} maxLength={120} required onChange={e => setJobName(e.target.value)} /></label>
          <details>
            <summary>高级参数</summary>
            <div className="training-params">
              {SETTINGS.map(field => (
                <label key={field.key}>{field.label}<input type="number" required value={params[field.key]}
                  min={field.min} max={field.max} step={field.step} onChange={e =>
                    setParams(previous => ({ ...previous, [field.key]: e.target.valueAsNumber }))} /></label>
              ))}
            </div>
          </details>
          <label className="training-check"><input type="checkbox" checked={allowBlank} onChange={e => setAllowBlank(e.target.checked)} />
            允许选中素材使用空描述</label>
          <p>预计 {picked.length * params.epochs * params.repeat} 步</p>
          <button className="training-primary"
            disabled={!check.data?.can_queue || !picked.length || !jobName.trim() || submit.isPending}>开始训练</button>
        </form>
      )}

      {viewingJobs && (
        <div className="training-jobs">
          <aside className="training-panel">
            <h2>训练任务</h2>
            {jobs.data?.map(item => <button key={item.id} aria-pressed={jobId === item.id}
              onClick={() => { setJobId(item.id); setDivider(50); setStatus('') }}>{item.name} · {RUN_STATUS[item.status] ?? item.status}</button>)}
            {!jobs.data?.length && !jobs.isLoading && <p>暂无训练任务</p>}
          </aside>
          {run && (
            <section className="training-panel">
              <h2>{run.name}</h2>
              <p>{RUN_STATUS[run.status] ?? run.status} · {run.step} / {run.total_steps} 步</p>
              <progress aria-label="训练进度" max={run.total_steps} value={Math.min(run.step, run.total_steps)} />
              {run.error && <p role="alert">{run.error}</p>}
              {run.started_at && <p>开始于 {new Date(run.started_at).toLocaleString()}</p>}
              {tab === '训练记录' && (
                <>
                  {ACTIVE.includes(run.status) && <button disabled={manageRun.isPending || run.status === 'cancelling'}
                    onClick={() => manageRun.mutate('cancel')}>取消训练</button>}
                  {RETRYABLE.includes(run.status) && <button disabled={manageRun.isPending}
                    onClick={() => manageRun.mutate('retry')}>使用原快照重试</button>}
                  <button disabled={!run.artifacts.length && run.kind !== 'preview'} onClick={() => setTab('效果预览')}>查看产物</button>
                  <pre aria-label="训练日志">{run.log || '暂无日志'}</pre>
                </>
              )}
              {tab === '效果预览' && (
                <>
                  {run.kind === 'preview' ? (
                    <>
                      {run.checkpoint && <p>{run.checkpoint} · 种子 {run.seed}</p>}
                      <p>{run.prompt}</p>
                      {run.status === 'completed' && (
                        <>
                          <div className="training-compare">
                            <img src={`${ROOT}/runs/${run.id}/preview/base`} alt="底模样图" />
                            <img src={`${ROOT}/runs/${run.id}/preview/lora`} alt="LoRA 样图"
                              style={{ clipPath: `inset(0 0 0 ${divider}%)` }} />
                            <span aria-hidden style={{ left: `${divider}%` }} />
                          </div>
                          <label>底模 / LoRA 对比<input type="range" min={0} max={100} value={divider}
                            onChange={e => setDivider(e.target.valueAsNumber)} /></label>
                          {run.published && <p>已发布到 {run.published}</p>}
                          <form className="training-toolbar" onSubmit={async e => {
                            e.preventDefault()
                            if (await confirmDialog({
                              message: `发布 LoRA 到 qwen2.1/${filename}.safetensors？`, confirmText: '发布 LoRA',
                            })) publish.mutate()
                          }}>
                            <label>发布文件名<input maxLength={100} required value={filename}
                              onChange={e => setFilename(e.target.value)} /></label>
                            <button disabled={!filename.trim() || publish.isPending}>发布 LoRA</button>
                          </form>
                        </>
                      )}
                    </>
                  ) : (
                    <>
                      <h3>Checkpoint</h3>
                      <ul>{run.artifacts.map(name => <li key={name}><a href={`${ROOT}/runs/${run.id}/artifacts/${name}`}>{name}</a></li>)}</ul>
                      {!run.artifacts.length && <p>暂无产物</p>}
                      {run.loss.length > 0 && (
                        <div className="training-loss" aria-label="训练 Loss 曲线">
                          <ResponsiveContainer height={190} width="100%">
                            <LineChart data={run.loss}>
                              <CartesianGrid stroke="var(--border)" />
                              <XAxis dataKey="step" />
                              <YAxis domain={['auto', 'auto']} width={52} />
                              <Tooltip contentStyle={{ color: 'var(--text)', background: 'var(--card)', borderColor: 'var(--border)' }} />
                              <Line dataKey="loss" name="Loss" stroke="var(--accent)" dot={false} isAnimationActive={false} />
                            </LineChart>
                          </ResponsiveContainer>
                        </div>
                      )}
                      {run.status === 'completed' && selectedCheckpoint && (
                        <form className="training-panel" onSubmit={e => { e.preventDefault(); createPreview.mutate(selectedCheckpoint) }}>
                          <label>选择 checkpoint<select value={selectedCheckpoint} onChange={e => setCheckpoint(e.target.value)}>
                            {run.artifacts.map(name => <option key={name}>{name}</option>)}
                          </select></label>
                          <label>对比提示词<textarea required maxLength={2000} value={previewPrompt}
                            onChange={e => setPreviewPrompt(e.target.value)} /></label>
                          <label>固定种子<input required type="number" min={0} max={4294967295} value={seed}
                            onChange={e => setSeed(e.target.valueAsNumber)} /></label>
                          <button disabled={!previewPrompt.trim() || createPreview.isPending}>生成底模与 LoRA 对比</button>
                        </form>
                      )}
                    </>
                  )}
                </>
              )}
            </section>
          )}
        </div>
      )}
    </main>
  )
}
