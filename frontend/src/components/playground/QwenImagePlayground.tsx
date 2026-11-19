import { useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Brush, Dices, Expand, Focus, Image, Play, ScanFace, ScanLine, Sparkles, Upload, UserRound, WandSparkles, X } from 'lucide-react'
import { paramKey, type ExposedParam } from '../../api/services'
import { Field } from './SchemaDrivenForm'
import { classifyField, defaultFor, isFieldRequired, isFieldVisible, optionDependency, staticOptions } from './fieldKind'
import ImageMaskDialog from './ImageMaskDialog'
import OutpaintFrame, { type OutpaintValues, type SourceDimensions } from './OutpaintFrame'
import './QwenImagePlayground.css'

const MODE_DETAILS = {
  moss_text: { icon: Sparkles, action: '用 MOSS 生图', help: '输入场景描述，设置 MOSS 画幅与生成像素。' },
  text: { icon: Sparkles, action: '生成图片', help: '用 Qwen Image 2.1 从提示词生图。' },
  reference: { icon: ScanLine, action: '反推并生图', help: '上传一张原图，再选择反推后用 MOSS 或 Qwen 重新生图。' },
  edit: { icon: WandSparkles, action: '编辑图片', help: '上传一至六张参考图，说明哪些元素要保留、替换或合并。' },
  oldx: { icon: ScanFace, action: '替换人物', help: '人物源图提供身份，姿势与场景参考提供构图、姿势和光线。' },
  character_card: { icon: UserRound, action: '生成人物资产卡', help: '上传人物源图，沿用内置资产卡的版式生成人物头肩与全身视图。' },
  outpaint: { icon: Expand, action: '扩展画面', help: '上传原图后拖动边框，可锁定宽度或高度。' },
  remove: { icon: Brush, action: '移除遮罩区域', help: '涂抹要移除的区域，也可上传同尺寸的黑白遮罩。' },
  upscale: { icon: Focus, action: '放大图片', help: '设置目标短边，使用 SeedVR2 放大原图。' },
} as const

type Preview = 'source' | 'result' | 'compare'
type SeedPolicy = 'fixed' | 'increment' | 'decrement' | 'randomize'

const advancedField = /^(?:seed|lora_|strength_|use_deepseek|ignore_edit_mask|moss_(steps|cfg|lora)|oldx_(strength|cfg))/
const referenceImage = /^image_[2-6]$/
const toImageUrl = (value: unknown) => typeof value === 'string' &&
  /^(data:image\/|\/|https?:\/\/)/i.test(value) ? value : undefined

function readImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.onload = () => resolve(String(reader.result))
    reader.readAsDataURL(file)
  })
}

function seedBounds(param: ExposedParam) {
  return {
    min: Number(param.constraints?.min ?? 0),
    max: Number(param.constraints?.max ?? 0xffffffff),
  }
}

function randomSeed(param: ExposedParam) {
  const { min, max } = seedBounds(param)
  const fraction = crypto.getRandomValues(new Uint32Array(1))[0] / 0x100000000
  return Math.floor(min + fraction * (max - min + 1))
}

export default function QwenImagePlayground({ inputs, initialInputs, resultUrl, activity, running, onRun, onModeChange }: {
  inputs: ExposedParam[]
  initialInputs?: Record<string, unknown>
  resultUrl?: string
  activity?: ReactNode
  running: boolean
  onRun: (values: Record<string, unknown>) => void
  onModeChange?: (mode: string) => void
}) {
  const [edits, setValues] = useState<Record<string, unknown>>({})
  const values = Object.fromEntries(inputs.flatMap(param => {
    const key = paramKey(param)
    if (!key) return []
    return [[key, key in edits ? edits[key] : initialInputs && key in initialInputs ? initialInputs[key] : defaultFor(param)]]
  }))
  const [natural, setNatural] = useState<{ url: string; size: SourceDimensions }>()
  const [preview, setPreview] = useState<Preview>('result')
  const [asset, setAsset] = useState('image')
  const [maskOpen, setMaskOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const [reading, setReading] = useState(false)
  const [seedPolicy, setSeedPolicy] = useState<SeedPolicy>(
    typeof initialInputs?.seed === 'number' ? 'fixed' : 'randomize',
  )
  const filePicker = useRef<HTMLInputElement>(null)
  const fileSlots = useRef<string[]>([])
  const queuedSeed = useRef<number | null>(null)

  const mode = typeof values.input_mode === 'string' ? values.input_mode : 'text'
  const active = inputs.filter(param => {
    const key = paramKey(param)
    if (!key || !isFieldVisible(param, values)) return false
    if (mode !== 'reference' || !Object.hasOwn(values, 'reference_engine')) return true
    if (key === 'moss_megapixels' || key === 'moss_aspect_ratio') return values.reference_engine === 'moss'
    if (key === 'megapixels' || key === 'aspect_ratio') return values.reference_engine !== 'moss'
    return true
  })
  const modeParam = active.find(param => paramKey(param) === 'input_mode')
  const modeDetail = MODE_DETAILS[mode as keyof typeof MODE_DETAILS]
  const candidates = active.flatMap(param => {
    const key = paramKey(param)!
    return key === 'image' || referenceImage.test(key) || key === 'mask' ||
      key === 'oldx_target' || key === 'oldx_reference' ? [{ key, param }] : []
  })
  const source = candidates.some(item => item.key === 'image') ? toImageUrl(values.image) : undefined
  const dims = natural?.url === source ? natural?.size : undefined
  const mask = toImageUrl(values.mask)
  const hasMask = candidates.some(item => item.key === 'mask')
  const enabled = !running && !reading
  const chosenAsset = candidates.some(item => item.key === asset) ? toImageUrl(values[asset]) : source
  const outpaintFields = active.filter(param => /^outpaint_(left|right|top|bottom)$/.test(paramKey(param)!))
  const hasOutpaint = mode === 'outpaint' && outpaintFields.length === 4
  const showPlan = hasOutpaint && source && dims && (preview === 'source' || !resultUrl)
  const controls = active.filter(param => param !== modeParam && !candidates.some(item => item.param === param) &&
    !(source && dims && hasOutpaint && outpaintFields.includes(param)))

  const update = (key: string, value: unknown) => {
    setValues(previous => ({ ...previous, [key]: value, ...(key === 'image' ? { mask: undefined } : {}) }))
    setNotice('')
  }

  const chooseAsset = (keys: string[]) => {
    fileSlots.current = keys
    filePicker.current?.click()
  }

  const loadFiles = async (files: File[], keys: string[]) => {
    if (!enabled || !files.length || !keys.length) return
    setReading(true)
    setNotice('')
    try {
      const chosen = files.slice(0, keys.length)
      if (chosen.some(file => !file.type.startsWith('image/'))) throw new Error('请选择图片文件')
      const images = await Promise.all(chosen.map(readImage))
      setValues(previous => {
        const next = { ...previous }
        for (const [index, image] of images.entries()) {
          next[keys[index]] = image
          if (keys[index] === 'image') next.mask = undefined
        }
        return next
      })
      setAsset(keys[0])
      setPreview('source')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : String(error))
    } finally {
      setReading(false)
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!enabled) return
    const payload: Record<string, unknown> = {}
    let usedSeed: { param: ExposedParam; value: number } | undefined
    for (const param of active) {
      const key = paramKey(param)!
      let value = values[key]
      if (key === 'seed') {
        const { min, max } = seedBounds(param)
        const raw = typeof value === 'number' ? value : min
        value = seedPolicy === 'randomize' ? queuedSeed.current ?? randomSeed(param) : Math.min(max, Math.max(min, raw))
        usedSeed = { param, value: Number(value) }
      }
      const missing = value === undefined || value === null || value === ''
      if (missing && isFieldRequired(param, values)) {
        setNotice(`请填写「${param.label || key}」`)
        return
      }
      if (missing && (value !== '' || /^(number|integer|slider|file)$/.test(classifyField(param)))) continue
      payload[key] = value
    }
    queuedSeed.current = null
    if (usedSeed) {
      const { min, max } = seedBounds(usedSeed.param)
      const value = usedSeed.value
      const next = seedPolicy === 'increment' ? (value >= max ? min : value + 1)
        : seedPolicy === 'decrement' ? (value <= min ? max : value - 1) : value
      update('seed', next)
    }
    setPreview('result')
    setNotice('')
    onRun(payload)
  }

  const renderField = (param: ExposedParam) => {
    const key = paramKey(param)!
    if (key === 'seed') {
      const { min, max } = seedBounds(param)
      const chooseRandom = () => {
        const seed = randomSeed(param)
        queuedSeed.current = seed
        setSeedPolicy('randomize')
        update('seed', seed)
      }
      return (
        <div className="qwen-seed" key={key}>
          <label>种子 <input type="number" min={min} max={max} step={1} value={(values.seed as number | undefined) ?? ''}
            readOnly={seedPolicy === 'randomize'}
            onChange={event => update('seed', event.currentTarget.value === '' ? '' : Number(event.currentTarget.value))} /></label>
          <button type="button" title="随机种子" aria-label="随机种子" onClick={chooseRandom}><Dices size={15} /></button>
          <select aria-label="种子模式" value={seedPolicy} onChange={event => {
            const choice = event.currentTarget.value as SeedPolicy
            if (choice === 'randomize') chooseRandom()
            else { queuedSeed.current = null; setSeedPolicy(choice) }
          }}>
            <option value="fixed">固定</option><option value="increment">递增</option>
            <option value="decrement">递减</option><option value="randomize">随机</option>
          </select>
        </div>
      )
    }
    if (classifyField(param) === 'boolean') {
      return <label key={key} className="qwen-toggle">
        <input type="checkbox" checked={Boolean(values[key])}
          onChange={event => update(key, event.currentTarget.checked)} />
        {param.label || key}
      </label>
    }
    const dependency = optionDependency(param)
    return <Field key={key} param={{ ...param, required: isFieldRequired(param, values) }} value={values[key]}
      dependsValue={dependency ? String(values[dependency.dependsOn] ?? '') : undefined}
      onChange={value => update(key, value)} />
  }

  const previewSource = chosenAsset || source
  const view = preview === 'compare' && resultUrl && source ? 'compare'
    : preview === 'result' && resultUrl ? 'result' : 'source'
  const advance = controls.filter(param => advancedField.test(paramKey(param)!))
  const primary = controls.filter(param => !advancedField.test(paramKey(param)!))
    .sort((a, b) => Number(paramKey(b) === 'reference_engine') - Number(paramKey(a) === 'reference_engine'))

  return (
    <form className="qwen-playground" onSubmit={submit}>
      {modeParam && <div className="qwen-modes" role="radiogroup" aria-label="功能模式">
        {staticOptions(modeParam).map(option => {
          const Glyph = MODE_DETAILS[option.value as keyof typeof MODE_DETAILS]?.icon ?? Image
          return <label key={String(option.value)}>
            <input type="radio" name="qwen-mode" checked={option.value === mode} disabled={!enabled}
              onChange={() => {
                update('input_mode', option.value)
                onModeChange?.(String(option.value))
                setPreview('source')
              }} />
            <Glyph size={15} />{option.label ?? String(option.value)}
          </label>
        })}
      </div>}
      <div className="qwen-workspace">
        <section className="qwen-panel" aria-label="运行参数">
          <header><strong>运行参数</strong></header>
          <fieldset disabled={!enabled} className="qwen-settings">
            {modeDetail && <p className="qwen-mode-help">{modeDetail.help}</p>}
            {!!candidates.length && <div className="qwen-assets">
              {candidates.map(({ key, param }) => {
                const image = toImageUrl(values[key])
                const title = key === 'image' ? (mode === 'oldx' || mode === 'character_card' ? '人物源图' : '原图')
                  : key === 'mask' ? '遮罩' : referenceImage.test(key) ? `参考图 ${key.split('_')[1]}` : param.label || key
                return (
                  <div className="qwen-asset" key={key}>
                    <button type="button" aria-pressed={key === asset && !!image}
                      onClick={() => { if (image) { setAsset(key); setPreview('source') } else chooseAsset([key]) }}>
                      {image ? <img src={image} alt="" /> : <Upload size={16} />}{title}
                      {isFieldRequired(param, values) && <span aria-label="必填">*</span>}
                    </button>
                    {image && <button type="button" title={`移除${title}`} aria-label={`移除${title}`}
                      onClick={() => update(key, undefined)}><X size={12} /></button>}
                  </div>
                )
              })}
            </div>}
            {hasMask && source && dims && <button type="button" className="qwen-edit-mask"
              onClick={() => setMaskOpen(true)}><Brush size={15} />{mask ? '编辑遮罩' : '绘制遮罩'}</button>}
            {primary.map(renderField)}
            {!!advance.length && <details><summary>高级参数</summary>{advance.map(renderField)}</details>}
          </fieldset>
          <footer>
            <span role="status">{notice}</span>
            <button type="submit" className="qwen-primary" disabled={!enabled}><Play size={15} />
              {running ? '运行中…' : modeDetail?.action ?? '运行'}
            </button>
          </footer>
        </section>
        <section className="qwen-panel" aria-label="图片预览">
          <header>
            <strong>预览</strong>
            <div className="qwen-preview-tabs">
              {([['source', '原图'], ['result', '结果'], ['compare', '对照']] as const).map(([id, title]) =>
                <button type="button" key={id} aria-pressed={view === id}
                  disabled={id === 'source' ? !previewSource : !resultUrl || (id === 'compare' && !source)}
                  onClick={() => setPreview(id)}>{title}</button>)}
            </div>
          </header>
          <div className="qwen-stage" onDragOver={event => {
            if (enabled && candidates.some(item => item.key === 'image') && event.dataTransfer.types.includes('Files')) event.preventDefault()
          }} onDrop={event => {
            if (!enabled || !candidates.some(item => item.key === 'image')) return
            event.preventDefault()
            const keys = candidates.filter(item => item.key !== 'mask').map(item => item.key)
            const empty = keys.filter(key => !toImageUrl(values[key]))
            void loadFiles(Array.from(event.dataTransfer.files), empty.length ? empty : keys)
          }}>
            {showPlan ? <OutpaintFrame key={source} image={source} dimensions={dims} values={Object.fromEntries(outpaintFields.map(param => [paramKey(param)!.slice(9), Number(values[paramKey(param)!]) || 0])) as OutpaintValues}
              limits={Object.fromEntries(outpaintFields.map(param => [paramKey(param)!.slice(9), Number(param.constraints?.max ?? 200)])) as OutpaintValues}
              disabled={!enabled} onChange={extent => setValues(previous => ({
                ...previous,
                ...Object.fromEntries(Object.entries(extent).map(([key, value]) => [`outpaint_${key}`, value])),
              }))} /> : view === 'compare' && source && resultUrl ? (
                <div className="qwen-compare">
                  <figure><img src={source} alt="原图" /><figcaption>原图</figcaption></figure>
                  <figure><img src={resultUrl} alt="生成结果" /><figcaption>结果</figcaption></figure>
                </div>
              ) : view === 'result' && resultUrl ? <img src={resultUrl} alt="生成结果" />
              : previewSource ? <img src={previewSource} alt={asset === 'mask' ? '遮罩' : '输入图片'} />
              : <div className="qwen-stage-empty"><Image size={24} />
                {candidates.some(item => item.key === 'image')
                  ? <button type="button" disabled={!enabled} onClick={() => chooseAsset(['image'])}>
                    {mode === 'oldx' || mode === 'character_card' ? '上传人物源图，或拖放到此处' : '上传原图，或拖放到此处'}
                  </button>
                  : <span>运行后在此查看图片</span>}
              </div>}
          </div>
          {(activity || resultUrl) && <div className="qwen-result-meta">{activity}
            {resultUrl && <a href={resultUrl} download>下载结果</a>}
          </div>}
        </section>
      </div>
      {source && <img key={source} src={source} alt="" hidden onLoad={event => {
        setNatural({ url: source, size: { width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight } })
      }} />}
      <input type="file" ref={filePicker} accept="image/*" multiple hidden onChange={event => {
        const chosen = Array.from(event.currentTarget.files ?? [])
        event.currentTarget.value = ''
        void loadFiles(chosen, fileSlots.current)
      }} />
      {maskOpen && source && dims && hasMask && <ImageMaskDialog source={source} dimensions={dims} existing={mask}
        onClose={() => setMaskOpen(false)} onSave={value => { update('mask', value); setMaskOpen(false) }} />}
    </form>
  )
}
