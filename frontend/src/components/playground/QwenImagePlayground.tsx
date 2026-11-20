import { useEffect, useEffectEvent, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { ArrowLeftRight, Brush, Dices, Expand, Focus, Image, Play, ScanFace, ScanLine, Sparkles, Upload, UserRound, WandSparkles, X } from 'lucide-react'
import { paramKey, type ExposedParam } from '../../api/services'
import { NodeSelectPopover } from '../nodes/NodeSelectPopover'
import { Field } from './SchemaDrivenForm'
import { classifyField, defaultFor, isFieldRequired, isFieldVisible, optionDependency, staticOptions } from './fieldKind'
import ImageMaskDialog from './ImageMaskDialog'
import OutpaintFrame, { type OutpaintValues, type SourceDimensions } from './OutpaintFrame'
import './QwenImagePlayground.css'

const COMIC_MODE = { value: 'comic_real', label: '漫｜真互转' } as const

const MODE_DETAILS = {
  [COMIC_MODE.value]: { icon: ArrowLeftRight, action: '转换图片', help: '在漫画风格和真人风格间转换。' },
  moss_text: { icon: Sparkles, action: '用 MOSS 生图', help: '输入场景描述，设置 MOSS 画幅与生成像素。' },
  text: { icon: Sparkles, action: '生成图片', help: '用 Qwen Image 2.1 从提示词生图。' },
  reference: { icon: ScanLine, action: '反推并生图', help: '上传一张原图，再选择反推后用 MOSS 或 Qwen 重新生图。' },
  edit: { icon: WandSparkles, action: '编辑图片', help: '上传一至六张参考图，说明哪些元素要保留、替换或合并。' },
  oldx: { icon: ScanFace, action: '替换人物', help: '人物源图提供身份，姿势与场景参考提供构图、姿势和光线。' },
  character_card: { icon: UserRound, action: '生成人物资产卡', help: '上传人物源图，沿用内置资产卡的版式生成人物头肩与全身视图。' },
  outpaint: { icon: Expand, action: '扩展画面', help: '上传原图后拖动边框，可锁定宽度或高度。' },
  remove: { icon: Brush, action: '移除遮罩区域', help: '标出要移除的区域，可附加提示词说明要清除什么。' },
  upscale: { icon: Focus, action: '放大图片', help: '设置目标短边，使用 SeedVR2 放大原图。' },
} as const

type Preview = 'source' | 'result' | 'compare'
type SeedPolicy = 'fixed' | 'increment' | 'decrement' | 'randomize'

const advancedField = /^(?:seed|ignore_edit_mask|moss_(steps|cfg)|oldx_cfg)/
const loraField = /^(?:(?:qwen|moss|oldx)_)?(?:lora|strength)(?:_|$)/
const controlOrder: Record<string, number> = {
  reference_engine: 0,
  prompt: 1,
  enable_optimization: 2,
  use_deepseek: 3,
  enable_upscale: 5,
  edit_upscale_resolution: 6,
}
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

function loraSlotKey(key: string) {
  if (key === 'oldx_strength') return 'oldx_lora_01'
  return key.replace(/^strength_/, 'lora_').replace(/_strength$/, '')
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
  const [showComicPlaceholder, setShowComicPlaceholder] = useState(false)
  const [comparison, setComparison] = useState<'side-by-side' | 'slider'>('side-by-side')
  const [split, setSplit] = useState(50)
  const [asset, setAsset] = useState('image')
  const [maskOpen, setMaskOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const [reading, setReading] = useState(false)
  const [seedPolicy, setSeedPolicy] = useState<SeedPolicy>(
    typeof initialInputs?.seed === 'number' ? 'fixed' : 'randomize',
  )
  const formRef = useRef<HTMLFormElement>(null)
  const filePicker = useRef<HTMLInputElement>(null)
  const fileSlots = useRef<string[]>([])
  const queuedSeed = useRef<number | null>(null)

  const mode = typeof values.input_mode === 'string' ? values.input_mode : 'text'
  const active = inputs.filter(param => {
    const key = paramKey(param)
    if (!key || !isFieldVisible(param, values)) return false
    if (key === 'use_deepseek' && mode !== 'reference' && values.enable_optimization === false) return false
    if (mode !== 'reference' || !Object.hasOwn(values, 'reference_engine')) return true
    if (key === 'moss_megapixels' || key === 'moss_aspect_ratio') return values.reference_engine === 'moss'
    if (key === 'megapixels' || key === 'aspect_ratio') return values.reference_engine !== 'moss'
    if (key === 'moss_loras') return values.reference_engine === 'moss'
    if (key === 'qwen_loras') return values.reference_engine !== 'moss'
    return true
  })
  const modeParam = active.find(param => paramKey(param) === 'input_mode')
  const modeOptions = modeParam ? staticOptions(modeParam) : []
  const comicAvailable = modeOptions.some(option => option.value === COMIC_MODE.value)
  const comicPlaceholder = showComicPlaceholder && !comicAvailable
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
  const loraLists = active.filter(param => param.type === 'lora_list')
  const controls = active.filter(param => param.type !== 'lora_list' && param !== modeParam && !candidates.some(item => item.param === param) &&
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

  const imageSlots = candidates.filter(item => item.key !== 'mask').map(item => item.key)
  const emptySlots = imageSlots.filter(key => !toImageUrl(values[key]))
  const availableSlots = emptySlots.length ? emptySlots : imageSlots

  const pasteImages = useEffectEvent((event: ClipboardEvent) => {
    if (!enabled || comicPlaceholder || maskOpen || event.defaultPrevented || !event.clipboardData) return
    const target = event.target
    if (target instanceof HTMLElement && target !== document.body) {
      if (!formRef.current?.contains(target)) return
      if (target.isContentEditable || target.closest('textarea, input:not([type=radio],[type=checkbox],[type=range])')) return
    }
    const focused = target instanceof Element ? target.closest<HTMLElement>('[data-image-slot]')?.dataset.imageSlot : undefined
    const slots = focused && candidates.some(item => item.key === focused) ? [focused] : availableSlots
    if (!slots.length) return
    const files = Array.from(event.clipboardData.files).filter(file => file.type.startsWith('image/'))
    if (!files.length) {
      for (const item of Array.from(event.clipboardData.items)) {
        if (item.kind !== 'file' || !item.type.startsWith('image/')) continue
        const file = item.getAsFile()
        if (file) files.push(file)
      }
    }
    if (!files.length) return
    event.preventDefault()
    event.stopPropagation()
    void loadFiles(files, slots)
  })

  useEffect(() => {
    const onPaste = (event: ClipboardEvent) => pasteImages(event)
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!enabled || comicPlaceholder) return
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
          <NodeSelectPopover appearance="console" topLayer ariaLabel="种子模式" width={82}
            value={seedPolicy} onChange={value => {
            const choice = value as SeedPolicy
            if (choice === 'randomize') chooseRandom()
            else { queuedSeed.current = null; setSeedPolicy(choice) }
          }} options={[
            { value: 'fixed', label: '固定' }, { value: 'increment', label: '递增' },
            { value: 'decrement', label: '递减' }, { value: 'randomize', label: '随机' },
          ]} />
        </div>
      )
    }
    if (key === 'use_deepseek') {
      return <div key={key} className="qwen-optimizer">
        <span>提示词优化模型</span>
        <NodeSelectPopover appearance="console" topLayer ariaLabel="提示词优化模型"
          value={values[key] === true ? 'deepseek' : 'local'}
          onChange={value => update(key, value === 'deepseek')}
          options={[{ value: 'local', label: '本机 Qwen' }, { value: 'deepseek', label: 'DeepSeek' }]} />
      </div>
    }
    if (classifyField(param) === 'boolean') {
      return <label key={key} className="qwen-toggle">
        <input type="checkbox" checked={Boolean(values[key])}
          onChange={event => update(key, event.currentTarget.checked)} />
        {param.label || key}
      </label>
    }
    if (key === 'prompt' && mode === 'remove') {
      return <label key={key} className="qwen-optimizer">
        <span>移除要求（选填）</span>
        <textarea rows={3} value={typeof values[key] === 'string' ? values[key] : ''}
          placeholder="例如：移除手中的物品，保留人物和光影"
          onChange={event => update(key, event.currentTarget.value)} />
      </label>
    }
    const dependency = optionDependency(param)
    return <Field key={key} topLayerSelect param={{ ...param, required: isFieldRequired(param, values) }} value={values[key]}
      dependsValue={dependency ? String(values[dependency.dependsOn] ?? '') : undefined}
      onChange={value => update(key, value)} />
  }

  const previewSource = chosenAsset || source
  const view = preview === 'compare' && resultUrl && source ? 'compare'
    : preview === 'result' && resultUrl ? 'result' : 'source'
  const primary: ExposedParam[] = []
  const advanced: ExposedParam[] = []
  const loraSlots = new Map<string, ExposedParam[]>()
  for (const param of controls) {
    const key = paramKey(param)!
    if (loraField.test(key)) {
      const slot = loraSlotKey(key)
      const fields = loraSlots.get(slot) ?? []
      if (key === slot) fields.unshift(param)
      else fields.push(param)
      loraSlots.set(slot, fields)
    } else if (advancedField.test(key)) advanced.push(param)
    else primary.push(param)
  }
  primary.sort((a, b) => (controlOrder[paramKey(a)!] ?? 4) - (controlOrder[paramKey(b)!] ?? 4))

  return (
    <form ref={formRef} className="qwen-playground" onSubmit={submit}>
      {modeParam && <div className="qwen-modes" role="radiogroup" aria-label="功能模式">
        {(comicAvailable ? modeOptions : [...modeOptions, COMIC_MODE]).map(option => {
          const isComic = option.value === COMIC_MODE.value
          const Glyph = MODE_DETAILS[option.value as keyof typeof MODE_DETAILS]?.icon ?? Image
          return <label key={String(option.value)}>
            <input type="radio" name="qwen-mode" checked={comicPlaceholder ? isComic : option.value === mode} disabled={!enabled}
              onChange={() => {
                const placeholder = isComic && !comicAvailable
                setShowComicPlaceholder(placeholder)
                if (placeholder || option.value === mode) return
                update('input_mode', option.value)
                onModeChange?.(String(option.value))
                setPreview('source')
              }} />
            <Glyph size={15} />{isComic ? COMIC_MODE.label : option.label ?? String(option.value)}
          </label>
        })}
      </div>}
      {comicPlaceholder ? (
        <section className="qwen-panel" aria-label={COMIC_MODE.label}>
          <header><strong>{COMIC_MODE.label}</strong></header>
          <div className="qwen-stage">
            <div className="qwen-stage-empty">
              <ArrowLeftRight size={24} aria-hidden="true" />
              <span>等待 ComfyUI 工作流接入</span>
            </div>
          </div>
        </section>
      ) : <div className="qwen-workspace">
        <section className="qwen-panel qwen-parameters" aria-label="运行参数">
          <header><strong>运行参数</strong></header>
          <div className="qwen-parameter-scroll" role="region" aria-label="运行参数列表" tabIndex={0}>
            <fieldset disabled={!enabled} className="qwen-settings">
              {modeDetail && <p className="qwen-mode-help">{modeDetail.help}</p>}
              {!!candidates.length && <div className="qwen-assets">
                {candidates.map(({ key, param }) => {
                  const image = toImageUrl(values[key])
                  const title = key === 'image' ? (mode === 'oldx' || mode === 'character_card' ? '人物源图' : '原图')
                    : key === 'mask' ? '遮罩' : referenceImage.test(key) ? `参考图 ${key.split('_')[1]}` : param.label || key
                  return (
                    <div className="qwen-asset" key={key}>
                      <button type="button" data-image-slot={key} aria-pressed={key === asset && !!image}
                        onClick={() => { if (image) { setAsset(key); setPreview('source') } else chooseAsset([key]) }}>
                        {image ? <img src={image} alt="" /> : <Upload size={16} />}
                        <span className="qwen-asset-label">
                          <span>{title}</span>
                          {isFieldRequired(param, values) && <span className="qwen-asset-required">必填</span>}
                        </span>
                      </button>
                      {image && <button type="button" title={`移除${title}`} aria-label={`移除${title}`}
                        onClick={() => update(key, undefined)}><X size={12} /></button>}
                    </div>
                  )
                })}
              </div>}
              {!!candidates.length && <p className="qwen-mode-help">也可按 Ctrl+V / ⌘V 粘贴图片</p>}
              {hasMask && source && dims && <button type="button" className="qwen-edit-mask"
                onClick={() => setMaskOpen(true)}><Brush size={15} />{mask ? '编辑遮罩' : '绘制遮罩'}</button>}
              {primary.map(renderField)}
              {(advanced.length > 0 || loraSlots.size > 0) && <details>
                <summary>高级参数</summary>
                {advanced.map(renderField)}
                {loraSlots.size > 0 && <div className="qwen-lora-fields" role="group" aria-label="LoRA">
                  <strong>LoRA</strong>
                  {Array.from(loraSlots.values()).flat().map(renderField)}
                </div>}
              </details>}
            </fieldset>
            {loraLists.length > 0 && <fieldset disabled={!enabled} className="qwen-settings">
              <details><summary>LoRA</summary>{loraLists.map(renderField)}</details>
            </fieldset>}
          </div>
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
          {view === 'compare' && <div className="qwen-compare-modes" aria-label="对照方式">
            <button type="button" aria-pressed={comparison === 'side-by-side'}
              onClick={() => setComparison('side-by-side')}>并排</button>
            <button type="button" aria-pressed={comparison === 'slider'}
              onClick={() => setComparison('slider')}>滑动对比</button>
          </div>}
          <div className="qwen-stage" onDragOver={event => {
            if (enabled && candidates.some(item => item.key === 'image') && event.dataTransfer.types.includes('Files')) event.preventDefault()
          }} onDrop={event => {
            if (!enabled || !candidates.some(item => item.key === 'image')) return
            event.preventDefault()
            void loadFiles(Array.from(event.dataTransfer.files), availableSlots)
          }}>
            {showPlan ? <OutpaintFrame key={source} image={source} dimensions={dims} values={Object.fromEntries(outpaintFields.map(param => [paramKey(param)!.slice(9), Number(values[paramKey(param)!]) || 0])) as OutpaintValues}
              limits={Object.fromEntries(outpaintFields.map(param => [paramKey(param)!.slice(9), Number(param.constraints?.max ?? 200)])) as OutpaintValues}
              disabled={!enabled} onChange={extent => setValues(previous => ({
                ...previous,
                ...Object.fromEntries(Object.entries(extent).map(([key, value]) => [`outpaint_${key}`, value])),
              }))} /> : view === 'compare' && source && resultUrl ? (
              comparison === 'slider' ? (
                <div className="qwen-compare-slider">
                  <img src={resultUrl} alt="生成结果" draggable={false} />
                  <img src={source} alt="原图" draggable={false} style={{ clipPath: `inset(0 ${100 - split}% 0 0)` }} />
                  <span className="qwen-compare-label source">原图</span>
                  <span className="qwen-compare-label result">结果</span>
                  <div className="qwen-compare-divider" style={{ left: `${split}%` }} aria-hidden="true">
                    <span>↔</span>
                  </div>
                  <input className="qwen-compare-range" aria-label="拖动分隔线比较原图与结果" type="range"
                    min={0} max={100} value={split} onChange={event => setSplit(Number(event.currentTarget.value))} />
                </div>
              ) : (
                <div className="qwen-compare">
                  <figure><img src={source} alt="原图" /><figcaption>原图</figcaption></figure>
                  <figure><img src={resultUrl} alt="生成结果" /><figcaption>结果</figcaption></figure>
                </div>
              )
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
      </div>}
      {source && <img key={source} src={source} alt="" hidden onLoad={event => {
        setNatural({ url: source, size: { width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight } })
      }} />}
      <input type="file" ref={filePicker} accept="image/*" multiple hidden onChange={event => {
        const chosen = Array.from(event.currentTarget.files ?? [])
        event.currentTarget.value = ''
        void loadFiles(chosen, fileSlots.current)
      }} />
      {!comicPlaceholder && maskOpen && source && dims && hasMask && <ImageMaskDialog source={source} dimensions={dims} existing={mask}
        onClose={() => setMaskOpen(false)} onSave={value => { update('mask', value); setMaskOpen(false) }} />}
    </form>
  )
}
