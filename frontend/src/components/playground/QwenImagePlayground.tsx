import { useEffect, useEffectEvent, useRef, useState, type DragEvent, type FormEvent, type ReactNode, type SyntheticEvent } from 'react'
import { ArrowLeftRight, Brush, Dices, Expand, Focus, Image, Images, Layers, Maximize2, Minimize2, Play, ScanFace, ScanLine, Sparkles, Upload, UserRound, WandSparkles, X } from 'lucide-react'
import { paramKey, type ExposedParam } from '../../api/services'
import { NodeSelectPopover } from '../nodes/NodeSelectPopover'
import { Field } from './SchemaDrivenForm'
import { classifyField, defaultFor, isFieldRequired, isFieldVisible, loraDirectory, optionDependency, staticOptions } from './fieldKind'
import GalleryImagePicker from './GalleryImagePicker'
import ImageMaskDialog from './ImageMaskDialog'
import OutpaintFrame, { type OutpaintValues, type SourceDimensions } from './OutpaintFrame'
import './QwenImagePlayground.css'

const COMIC_MODE = { value: 'comic_real', label: '漫｜真互转' } as const

const QWEN_TURBO = 'Qwen Image 2.1 Turbo'
const QWEN_BASE = 'Qwen Image 2.1（基础版）'
const MOSS = 'MOSS Image'
const TEXT_MODELS: Record<string, string> = { moss_text: MOSS, text: QWEN_TURBO }
const REFERENCE_MODELS: Record<string, string> = { moss: MOSS, qwen: QWEN_TURBO }
const TOOL_MODELS: Record<string, string> = {
  edit: QWEN_TURBO, outpaint: QWEN_TURBO, remove: QWEN_TURBO,
  oldx: QWEN_BASE, character_card: QWEN_BASE, upscale: 'SeedVR2',
}

const MODE_DETAILS = {
  [COMIC_MODE.value]: { icon: ArrowLeftRight, action: '转换图片', help: '在漫画风格和真人风格间转换。' },
  moss_text: { icon: Sparkles, action: '用 MOSS 生图', help: '输入场景描述，设置 MOSS 画幅与生成像素。' },
  text: { icon: Sparkles, action: '生成图片', help: '用 Qwen Image 2.1 Turbo 从提示词生图。' },
  reference: { icon: ScanLine, action: '反推并生图', help: '上传一张原图，再选择反推后用 MOSS 或 Qwen 重新生图。' },
  edit: { icon: WandSparkles, action: '编辑图片', help: '上传一至六张参考图，说明哪些元素要保留、替换或合并。' },
  oldx: { icon: ScanFace, action: '替换人物', help: '人物源图提供身份，姿势与场景参考提供构图、姿势和光线。' },
  character_card: { icon: UserRound, action: '生成人物资产卡', help: '上传人物源图，沿用内置资产卡的版式生成人物头肩与全身视图。' },
  outpaint: { icon: Expand, action: '扩展画面', help: '上传原图后拖动边框，可锁定宽度或高度。' },
  remove: { icon: Brush, action: '移除遮罩区域', help: '标出要移除的区域，可附加提示词说明要清除什么。' },
  upscale: { icon: Focus, action: '放大图片', help: '设置目标短边，使用 SeedVR2 放大原图。' },
} as const

const MODE_GROUPS = [
  { name: '生图与反推', icon: Sparkles, modes: [...Object.keys(TEXT_MODELS), 'reference'] },
  { name: '单图／多图编辑', icon: WandSparkles, modes: ['edit', 'comic_real'] },
  { name: '人物工具', icon: UserRound, modes: ['oldx', 'character_card'] },
  { name: '扩图', icon: Expand, modes: ['outpaint'] },
  { name: '遮罩移除', icon: Brush, modes: ['remove'] },
  { name: '图片放大', icon: Focus, modes: ['upscale'] },
]

type Preview = 'source' | 'result' | 'compare'
type SeedPolicy = 'fixed' | 'increment' | 'decrement' | 'randomize'

const advancedField = /^(?:seed|enable_optimization|use_deepseek|prompt_skill_|ignore_edit_mask|moss_(steps|cfg)|oldx_cfg)/
const loraField = /^(?:(?:qwen|moss|oldx)_)?(?:lora|strength)(?:_|$)/
const controlOrder: Record<string, number> = {
  prompt: 1,
  enable_optimization: 2,
  use_deepseek: 3,
  enable_upscale: 5,
  edit_upscale_resolution: 6,
}
const referenceImage = /^image_[2-6]$/
const toImageUrl = (value: unknown) => typeof value === 'string' &&
  /^(data:image\/|\/|https?:\/\/)/i.test(value) ? value : undefined

function readImage(file: Blob): Promise<string> {
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
  const [generated, setGenerated] = useState<{ url: string; size: SourceDimensions }>()
  const [fullscreen, setFullscreen] = useState<'native' | 'expanded' | null>(null)
  const [preview, setPreview] = useState<Preview>('result')
  const [showComicPlaceholder, setShowComicPlaceholder] = useState(false)
  const [comparison, setComparison] = useState<'side-by-side' | 'slider'>('side-by-side')
  const [lastTextMode, setLastTextMode] = useState<string>()
  const [parameterTab, setParameterTab] = useState<'basic' | 'advanced'>('basic')
  const [split, setSplit] = useState(50)
  const [asset, setAsset] = useState('image')
  const [maskOpen, setMaskOpen] = useState(false)
  const [gallerySlot, setGallerySlot] = useState<string>()
  const [notice, setNotice] = useState('')
  const [reading, setReading] = useState(false)
  const [seedPolicy, setSeedPolicy] = useState<SeedPolicy>(
    typeof initialInputs?.seed === 'number' ? 'fixed' : 'randomize',
  )
  const formRef = useRef<HTMLFormElement>(null)
  const previewRef = useRef<HTMLElement>(null)
  const filePicker = useRef<HTMLInputElement>(null)
  const fileSlots = useRef<string[]>([])
  const loadingFiles = useRef(false)
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
  const textOptions = modeOptions.flatMap(({ value }) => {
    const key = String(value)
    return Object.hasOwn(TEXT_MODELS, key) ? [{ value: key, label: TEXT_MODELS[key] }] : []
  })
  const referenceParam = active.find(param => paramKey(param) === 'reference_engine')
  const referenceOptions = referenceParam ? staticOptions(referenceParam).map(({ value, label }) => {
    const key = String(value)
    return { value: key, label: REFERENCE_MODELS[key] ?? label ?? key }
  }) : []
  const referenceModel = String(values.reference_engine ?? 'qwen')
  const currentModel = mode === 'reference'
    ? referenceOptions.find(option => option.value === referenceModel)?.label ?? REFERENCE_MODELS[referenceModel]
    : TEXT_MODELS[mode] ?? TOOL_MODELS[mode]
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
  const selectedAsset = candidates.find(item => item.key === asset && toImageUrl(values[asset]))
  const chosenAsset = selectedAsset ? toImageUrl(values[asset]) : undefined
  const outpaintFields = active.filter(param => /^outpaint_(left|right|top|bottom)$/.test(paramKey(param)!))
  const hasOutpaint = mode === 'outpaint' && outpaintFields.length === 4
  const showPlan = hasOutpaint && source && dims && (preview === 'source' || !resultUrl)
  const loraLists = active.filter(param => param.type === 'lora_list')
  const controls = active.filter(param => param.type !== 'lora_list' && param !== modeParam && param !== referenceParam && !candidates.some(item => item.param === param) &&
    !(source && dims && hasOutpaint && outpaintFields.includes(param)))

  const update = (key: string, value: unknown) => {
    setValues(previous => ({ ...previous, [key]: value, ...(key === 'image' ? { mask: undefined } : {}) }))
    setNotice('')
  }

  const chooseAsset = (keys: string[]) => {
    fileSlots.current = keys
    filePicker.current?.click()
  }

  const loadFiles = async (files: Blob[], keys: string[]) => {
    if (!enabled || loadingFiles.current || !files.length || !keys.length) return
    loadingFiles.current = true
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
      loadingFiles.current = false
      setReading(false)
    }
  }

  const imageSlots = candidates.filter(item => item.key !== 'mask').map(item => item.key)
  const emptySlots = imageSlots.filter(key => !toImageUrl(values[key]))
  const availableSlots = emptySlots.length ? emptySlots : imageSlots
  const materialSlot = candidates.find(item => item.key === asset) ?? candidates[0]
  const getImageSlot = (target: EventTarget | null) => {
    const key = target instanceof Element ? target.closest<HTMLElement>('[data-image-slot]')?.dataset.imageSlot : undefined
    return candidates.find(item => item.key === key)?.key
  }

  const acceptDrop = (event: DragEvent) => {
    if (candidates.length && event.dataTransfer.types.includes('Files')) {
      event.preventDefault()
      event.dataTransfer.dropEffect = enabled ? 'copy' : 'none'
    }
  }
  const dropImages = (event: DragEvent) => {
    if (!candidates.length || !event.dataTransfer.types.includes('Files')) return
    event.preventDefault()
    if (!enabled || gallerySlot || maskOpen) return
    const slot = getImageSlot(event.target)
    const files = Array.from(event.dataTransfer.files)
    const slots = slot ? [slot] : files.length === 1 && materialSlot ? [materialSlot.key] : availableSlots
    void loadFiles(files, slots)
  }

  const pasteImages = useEffectEvent((event: ClipboardEvent) => {
    if (!enabled || comicPlaceholder || gallerySlot || maskOpen || event.defaultPrevented || !event.clipboardData) return
    const target = event.target
    if (target instanceof HTMLElement && target !== document.body) {
      if (!formRef.current?.contains(target)) return
      if (target.isContentEditable || target.closest('textarea, input:not([type=radio],[type=checkbox],[type=range])')) return
    }
    const focused = getImageSlot(target)
    const files = Array.from(event.clipboardData.files).filter(file => file.type.startsWith('image/'))
    if (!files.length) {
      for (const item of Array.from(event.clipboardData.items)) {
        if (item.kind !== 'file' || !item.type.startsWith('image/')) continue
        const file = item.getAsFile()
        if (file) files.push(file)
      }
    }
    const slots = focused ? [focused] : files.length === 1 && materialSlot ? [materialSlot.key] : availableSlots
    if (!files.length || !slots.length) return
    event.preventDefault()
    event.stopPropagation()
    void loadFiles(files, slots)
  })

  useEffect(() => {
    const onPaste = (event: ClipboardEvent) => pasteImages(event)
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [])

  useEffect(() => {
    const onFullscreen = () => setFullscreen(current =>
      document.fullscreenElement && document.fullscreenElement === previewRef.current ? 'native'
        : current === 'native' ? null : current)
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setFullscreen(current => current === 'expanded' ? null : current)
    }
    document.addEventListener('fullscreenchange', onFullscreen)
    document.addEventListener('keydown', onEscape)
    return () => {
      document.removeEventListener('fullscreenchange', onFullscreen)
      document.removeEventListener('keydown', onEscape)
    }
  }, [])

  const toggleFullscreen = async () => {
    if (fullscreen) {
      if (fullscreen === 'native' && document.fullscreenElement) await document.exitFullscreen()
      else setFullscreen(null)
      return
    }
    const panel = previewRef.current
    if (panel?.requestFullscreen) {
      try {
        await panel.requestFullscreen()
        setFullscreen('native')
        return
      } catch {
        setFullscreen('expanded')
        return
      }
    }
    setFullscreen('expanded')
  }

  const recordResult = (event: SyntheticEvent<HTMLImageElement>) => {
    const { naturalWidth: width, naturalHeight: height } = event.currentTarget
    if (resultUrl && width && height) setGenerated({ url: resultUrl, size: { width, height } })
  }

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
    const directory = loraDirectory(key)
    const constraints = param.constraints
    const choices = constraints?.enum
    const accepts = (name: unknown) => typeof name === 'string' && (!directory || name === 'None' || name.toLowerCase().startsWith(directory))
    const filtered = directory && Array.isArray(choices)
      ? { ...constraints, enum: choices.filter(accepts), option_meta: staticOptions(param).filter(option => accepts(option.value)) }
      : constraints
    return <Field key={key} topLayerSelect param={{ ...param, constraints: filtered, required: isFieldRequired(param, values) }} value={values[key]}
      dependsValue={dependency ? String(values[dependency.dependsOn] ?? '') : undefined}
      onChange={value => update(key, value)} />
  }

  const previewSource = chosenAsset || source
  const view = preview === 'compare' && resultUrl && previewSource ? 'compare'
    : preview === 'result' && resultUrl ? 'result' : 'source'
  const resultSize = generated?.url === resultUrl ? generated?.size : undefined
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

  const availableModes = comicAvailable ? modeOptions : [...modeOptions, COMIC_MODE]
  const currentMode = comicPlaceholder ? COMIC_MODE.value : mode
  const groups = MODE_GROUPS.map(group => ({ ...group,
    options: availableModes.filter(option => group.modes.includes(String(option.value))),
  })).filter(group => group.options.length > 0)
  for (const option of availableModes) {
    const value = String(option.value)
    if (!groups.some(group => group.modes.includes(value))) {
      groups.push({ name: option.label ?? value, icon: Image, modes: [value], options: [option] })
    }
  }
  const currentGroup = groups.find(group => group.modes.includes(currentMode))
  const chooseMode = (next: string) => {
    const placeholder = next === COMIC_MODE.value && !comicAvailable
    setShowComicPlaceholder(placeholder)
    if (placeholder || next === mode) return
    if (textOptions.some(option => option.value === next)) setLastTextMode(next)
    else if (textOptions.some(option => option.value === mode)) setLastTextMode(mode)
    update('input_mode', next)
    onModeChange?.(next)
    setPreview('source')
    setParameterTab('basic')
  }
  const assetTitle = (key: string, param?: ExposedParam) => key === 'image'
    ? (mode === 'oldx' || mode === 'character_card' ? '人物源图' : '原图')
    : key === 'mask' ? '遮罩' : referenceImage.test(key) ? `参考图 ${key.split('_')[1]}` : param?.label || key
  const comparisonTitle = selectedAsset ? assetTitle(asset, selectedAsset.param) : assetTitle('image')
  const hasAdvanced = advanced.length > 0 || loraSlots.size > 0 || loraLists.length > 0
  const showAdvanced = hasAdvanced && parameterTab === 'advanced'

  const textMode = textOptions.find(option => option.value === lastTextMode)?.value ?? textOptions[0]?.value
  const isReference = currentMode === 'reference'
  const generationGroup = isReference || textOptions.some(option => option.value === currentMode)
  const referenceAvailable = modeOptions.some(option => option.value === 'reference')
  const submodes = generationGroup
    ? [
        ...(textMode ? [{ value: textMode, label: '文字生图', checked: !isReference }] : []),
        ...(referenceAvailable ? [{ value: 'reference', label: '图片反推', checked: isReference }] : []),
      ]
    : (currentGroup?.options ?? []).map(option => ({
        value: String(option.value), label: option.label ?? String(option.value), checked: currentMode === option.value,
      }))
  const modeTabs = submodes.length > 1 && (
    <div className="qwen-submodes" role="radiogroup" aria-label="功能选项">
      {submodes.map(({ value, label, checked }) => <label key={label}>
        <input type="radio" name="qwen-mode" checked={checked} disabled={!enabled}
          onChange={() => chooseMode(value)} />
        {label}
      </label>)}
    </div>
  )
  const modelPicker = generationGroup && (isReference ? referenceOptions.length > 0 : textOptions.length > 0) && (
    <div className="qwen-model-picker">
      <span>{isReference ? '反推后使用的生图模型' : '模型'}</span>
      <NodeSelectPopover appearance="console" topLayer disabled={!enabled}
        ariaLabel={isReference ? '反推后使用的生图模型' : '模型'}
        value={isReference ? referenceModel : mode}
        options={isReference ? referenceOptions : textOptions}
        onChange={isReference ? value => update('reference_engine', value) : chooseMode} />
    </div>
  )

  return (
    <form ref={formRef} className="qwen-playground" onSubmit={submit}>
      {modeParam && <nav className="qwen-modes" aria-label="功能分类">
        {groups.map(({ name, icon: Glyph, modes, options }) =>
          <button key={name} type="button" aria-pressed={modes.includes(currentMode)} disabled={!enabled}
            onClick={() => { if (!modes.includes(currentMode)) chooseMode(String(options[0].value)) }}>
            <Glyph size={15} />{name}
          </button>)}
      </nav>}
      {comicPlaceholder ? (
        <section className="qwen-panel" aria-label={COMIC_MODE.label}>
          <header>{modeTabs || <strong>{COMIC_MODE.label}</strong>}</header>
          <div className="qwen-stage">
            <div className="qwen-stage-empty">
              <ArrowLeftRight size={24} aria-hidden="true" />
              <span>等待 ComfyUI 工作流接入</span>
            </div>
          </div>
        </section>
      ) : <div className="qwen-workspace">
        <section className="qwen-panel qwen-materials" aria-label="素材" onDragOver={acceptDrop} onDrop={dropImages}>
          <header><Layers size={15} /><strong>素材</strong></header>
          <div className="qwen-parameter-scroll">
            <fieldset disabled={!enabled} className="qwen-settings">
              {!!candidates.length && <div className="qwen-assets">
                {candidates.map(({ key, param }) => {
                  const image = toImageUrl(values[key])
                  const title = assetTitle(key, param)
                  return (
                    <div className="qwen-asset" key={key}>
                      <button type="button" data-image-slot={key} aria-pressed={key === materialSlot?.key}
                        onClick={() => {
                          setAsset(key)
                          setPreview(previous => image && previous === 'compare' && resultUrl ? 'compare' : 'source')
                        }}>
                        {image ? <img src={image} alt="" /> : <Image size={16} />}
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
              {materialSlot && <>
                <div className="qwen-material-actions">
                  <button type="button" onClick={() => chooseAsset([materialSlot.key])}><Upload size={15} /> 本地上传</button>
                  <button type="button" aria-haspopup="dialog" onClick={() => setGallerySlot(materialSlot.key)}>
                    <Images size={15} /> 从图库选择
                  </button>
                </div>
                <p className="qwen-mode-help">可拖放文件，或按 Ctrl+V / ⌘V 粘贴图片</p>
              </>}
              {hasMask && source && dims && <button type="button" className="qwen-edit-mask"
                onClick={() => setMaskOpen(true)}><Brush size={15} />{mask ? '编辑遮罩' : '绘制遮罩'}</button>}
              {candidates.length === 0 && <p className="qwen-mode-help">此模式无需素材</p>}
            </fieldset>
          </div>
        </section>
        <section ref={previewRef} className={`qwen-panel qwen-preview${fullscreen === 'expanded' ? ' qwen-preview-expanded' : ''}`} aria-label="图片预览">
          <header>
            <div className="qwen-preview-heading">
              <strong>预览</strong>
              {currentModel && <span className="qwen-preview-model" aria-label={`当前模型 ${currentModel}`}>{currentModel}</span>}
            </div>
            <div className="qwen-preview-tabs">
              {([['source', '原图'], ['result', '结果'], ['compare', '对照']] as const).map(([id, title]) =>
                <button type="button" key={id} aria-pressed={view === id}
                  disabled={id === 'source' ? !previewSource : !resultUrl || (id === 'compare' && !previewSource)}
                  onClick={() => setPreview(id)}>{title}</button>)}
              <button type="button" aria-label={fullscreen ? '退出全屏' : '全屏预览'} title={fullscreen ? '退出全屏' : '全屏预览'}
                disabled={!fullscreen && !previewSource && !resultUrl} onClick={() => void toggleFullscreen()}>
                {fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
              </button>
            </div>
          </header>
          {running && activity && <div className="qwen-preview-progress">{activity}</div>}
          {view === 'compare' && <div className="qwen-compare-modes" aria-label="对照方式">
            <button type="button" aria-pressed={comparison === 'side-by-side'}
              onClick={() => setComparison('side-by-side')}>并排</button>
            <button type="button" aria-pressed={comparison === 'slider'}
              onClick={() => setComparison('slider')}>滑动对比</button>
          </div>}
          <div className="qwen-stage" onDragOver={acceptDrop} onDrop={dropImages}>
            {showPlan ? <OutpaintFrame key={source} image={source} dimensions={dims} values={Object.fromEntries(outpaintFields.map(param => [paramKey(param)!.slice(9), Number(values[paramKey(param)!]) || 0])) as OutpaintValues}
              limits={Object.fromEntries(outpaintFields.map(param => [paramKey(param)!.slice(9), Number(param.constraints?.max ?? 200)])) as OutpaintValues}
              disabled={!enabled} onChange={extent => setValues(previous => ({
                ...previous,
                ...Object.fromEntries(Object.entries(extent).map(([key, value]) => [`outpaint_${key}`, value])),
              }))} /> : view === 'compare' && previewSource && resultUrl ? (
              comparison === 'slider' ? (
                <div className="qwen-compare-slider">
                  <img src={resultUrl} alt="生成结果" onLoad={recordResult} draggable={false} />
                  <img src={previewSource} alt="原图" draggable={false} style={{ clipPath: `inset(0 ${100 - split}% 0 0)` }} />
                  <span className="qwen-compare-label source">{comparisonTitle}</span>
                  <span className="qwen-compare-label result">结果</span>
                  <div className="qwen-compare-divider" style={{ left: `${split}%` }} aria-hidden="true">
                    <span>↔</span>
                  </div>
                  <input className="qwen-compare-range" aria-label="拖动分隔线比较原图与结果" type="range"
                    min={0} max={100} value={split} onChange={event => setSplit(Number(event.currentTarget.value))} />
                </div>
              ) : (
                <div className="qwen-compare">
                  <figure><img src={previewSource} alt="原图" /><figcaption>{comparisonTitle}</figcaption></figure>
                  <figure><img src={resultUrl} alt="生成结果" onLoad={recordResult} /><figcaption>结果</figcaption></figure>
                </div>
              )
            ) : view === 'result' && resultUrl ? <img src={resultUrl} alt="生成结果" onLoad={recordResult} />
              : previewSource ? <img src={previewSource} alt={asset === 'mask' ? '遮罩' : '输入图片'} />
              : <div className="qwen-stage-empty"><Image size={24} />
                {candidates.some(item => item.key === 'image')
                  ? <button type="button" disabled={!enabled} onClick={() => chooseAsset(['image'])}>
                    {mode === 'oldx' || mode === 'character_card' ? '上传人物源图，或拖放到此处' : '上传原图，或拖放到此处'}
                  </button>
                  : <span>运行后在此查看图片</span>}
              </div>}
          </div>
          {((!running && activity) || resultUrl) && <div className="qwen-result-meta">
            {!running && activity}
            {resultUrl && <div className="qwen-result-details">
              {resultSize && view !== 'source' && <span aria-label="生成图片分辨率">
                {resultSize.width} × {resultSize.height} px · {(resultSize.width * resultSize.height / 1e6).toFixed(2)} MP
              </span>}
              <a href={resultUrl} download>下载结果</a>
            </div>}
          </div>}
        </section>
        <section className="qwen-panel qwen-parameters" aria-label="运行参数">
          <header>{modeTabs || <strong>运行参数</strong>}</header>
          {hasAdvanced && <div className="qwen-parameter-tabs" aria-label="参数分类">
            <button type="button" aria-pressed={!showAdvanced} onClick={() => setParameterTab('basic')}>基础参数</button>
            <button type="button" aria-pressed={showAdvanced} onClick={() => setParameterTab('advanced')}>高级参数</button>
          </div>}
          <div className="qwen-parameter-scroll" role="region" aria-label="运行参数列表" tabIndex={0}>
            <fieldset disabled={!enabled} className="qwen-settings">
              {!showAdvanced && modelPicker}
              {!showAdvanced && modeDetail && <p className="qwen-mode-help">{modeDetail.help}</p>}
              {(showAdvanced ? advanced : primary).map(renderField)}
              {showAdvanced && (loraSlots.size > 0 || loraLists.length > 0) &&
                <div className="qwen-lora-fields" role="group" aria-label="LoRA">
                  {loraSlots.size > 0 && <strong>LoRA</strong>}
                  {Array.from(loraSlots.values()).flat().map(renderField)}
                  {loraLists.map(renderField)}
                </div>}
            </fieldset>
          </div>
          <footer>
            <span role="status">{notice}</span>
            <button type="submit" className="qwen-primary" disabled={!enabled}><Play size={15} />
              {running ? '运行中…' : modeDetail?.action ?? '运行'}
            </button>
          </footer>
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
      {gallerySlot && <GalleryImagePicker
        destination={assetTitle(gallerySlot, candidates.find(item => item.key === gallerySlot)?.param)}
        onClose={() => setGallerySlot(undefined)}
        onSelect={file => {
          setGallerySlot(undefined)
          void loadFiles([file], [gallerySlot])
        }} />}
      {!comicPlaceholder && maskOpen && source && dims && hasMask && <ImageMaskDialog source={source} dimensions={dims} existing={mask}
        onClose={() => setMaskOpen(false)} onSave={value => { update('mask', value); setMaskOpen(false) }} />}
    </form>
  )
}
