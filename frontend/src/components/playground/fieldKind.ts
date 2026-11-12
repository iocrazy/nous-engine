import type { ExposedParam } from '../../api/services'
import { paramKey, paramSlot } from '../../api/services'

/** 字段类型判定与默认值推断 —— 从 SchemaDrivenForm.tsx 抽出来的纯函数。
 *
 * 为什么单独成文件:`ComfyTemplateEditor` 要复用 `defaultFor`(算「画布节点预览」
 * 左栏的字段初值),但从组件文件里导出非组件会打破 React Fast Refresh
 * (eslint `react-refresh/only-export-components`,CI 上是 error)。
 * 纯逻辑挪到这里,组件文件只导出组件。
 */
export type FieldKind =
  | 'string'
  | 'string_multiline'
  | 'number'
  | 'integer'
  | 'boolean'
  | 'radio'
  | 'file'
  | 'select'
  | 'thumb_select'
  | 'slider'

export function num(v: unknown): number | undefined {
  const n = Number(v)
  return Number.isFinite(n) ? n : undefined
}

export function classifyField(p: ExposedParam): FieldKind {
  const constraints = (p.constraints ?? {}) as Record<string, unknown>
  const t = (p.type ?? 'string').toLowerCase()
  // 文件类语义优先于 enum:ComfyUI 的 LoadImage 等节点经 /object_info 带出的
  // enum 是 sidecar 上已存在的文件名列表(如 ["5 (1).jpg","example.png"]),不是
  // 一个可选值域 —— 桥会把上传的 data URI 传给 sidecar 落盘,不是从这批名字里选一个。
  // 必须在 enum 判断之前拦掉,否则这类字段会被误判成下拉框,Playground 就没有上传控件。
  if (t === 'file' || t === 'image' || t === 'audio' || t === 'video' || t === 'binary' || t === 'media') {
    return 'file'
  }
  if (Array.isArray(constraints.enum) && constraints.enum.length > 0) {
    if ((t === 'boolean' || t === 'bool') && constraints.enum.length === 2
      && constraints.enum.includes(true) && constraints.enum.includes(false)) return 'radio'
    // option_meta 里只要有任何一项带 image,就用缩略图网格 —— 275 个 fooocus 风格
    // 用纯文本下拉根本没法挑(后端 comfy_templates.py::_split_options 写入)。
    // 一张图都没有时照旧退回 <select>,不做无谓的布局变更。
    const meta = constraints.option_meta
    if (Array.isArray(meta) && meta.some((m) => (m as { image?: unknown })?.image)) {
      return 'thumb_select'
    }
    return 'select'
  }
  // 静态 enum 是空的、但字段声明了「选项随另一个参数变」(options_source):清单在运行期
  // 才拉,注册时一项都没冻结。这仍然是个**选择器**,不是自由文本 —— 不在这里归类,
  // SchemaDrivenForm 的依赖分支(只对 select/thumb_select 生效)就挂不上,字段会渲成
  // 多行文本框、联动整条失效。网格还是下拉由 DependentOptionField 按**实际拉到的**
  // 清单有没有缩略图决定,所以这里统一给 'select'。
  if (optionDependency(p)) return 'select'
  const numeric = t === 'integer' || t === 'int' || t === 'number' || t === 'float'
  // Numeric field with a bounded range → slider (对齐 Infinite-Canvas / nous
  // 节点的 slider widget,min/max/step 从 ExposedParam.constraints 带出)。
  if (numeric && num(constraints.min) !== undefined && num(constraints.max) !== undefined) {
    return 'slider'
  }
  if (t === 'integer' || t === 'int') return 'integer'
  if (t === 'number' || t === 'float') return 'number'
  if (t === 'boolean' || t === 'bool') return 'boolean'
  // Strings default to multiline. Single-line is opt-in via constraints.format='single_line'
  // — published service inputs are almost always free text (prompts, transcripts,
  // user content) and a single 32-char input is unusable for those.
  if (constraints.format === 'single_line') return 'string'
  return 'string_multiline'
}

/** Seed-like numeric field → show a 🎲 randomize button. Opt-in via
 *  constraints.random, or auto-detected by a `seed` key/slot name. */
export function isRandomizable(p: ExposedParam): boolean {
  const constraints = (p.constraints ?? {}) as Record<string, unknown>
  if (constraints.random === true) return true
  const name = `${paramKey(p) ?? ''} ${paramSlot(p) ?? ''}`.toLowerCase()
  return /seed/.test(name)
}

/** exported: ComfyTemplateEditor 的「画布节点预览」左栏复用它算字段初值,不用重复
 *  一套 default 推断逻辑(该逻辑 defaultFor 就已经跟 classifyField 的字段类型判定绑死)。 */
export function defaultFor(p: ExposedParam): unknown {
  if (p.default !== undefined && p.default !== null) return p.default
  switch (classifyField(p)) {
    case 'boolean':
    case 'radio':
      return false
    case 'slider': {
      const c = (p.constraints ?? {}) as Record<string, unknown>
      return num(c.min) ?? 0
    }
    case 'number':
    case 'integer':
      return ''
    case 'file':
      return null
    default:
      return ''
  }
}



/** 逗号串 → 值数组。空串 → 空数组(不是 [''])。 */
export function parseValues(v: string): string[] {
  return v.split(',').map((s) => s.trim()).filter(Boolean)
}



/** 一个 combo 选项(与 OptionThumbGrid 的 `OptionMeta` 同形,这里另写一份纯类型
 *  声明是为了不让 fieldKind.ts 依赖组件文件)。 */
export interface FieldOption {
  value: string
  label?: string
  image?: string
}

export interface OptionDependency {
  /** 依赖的那个参数的 key —— 它的当前值就是拉清单用的入参(如风格包名)。 */
  dependsOn: string
  /** 清单来源。目前只有 `comfy_styles`。 */
  source: string
}

/** 该字段的选项是否依赖另一个参数;不依赖 → null。
 *
 *  认两种形状:Playground 拿的是 `/api/v1/services/{id}` 的 exposed_inputs
 *  (`constraints.options_depends_on`),第三方按 `/v1/services/{name}/schema` 集成
 *  拿的是 JSON-Schema 扩展关键字(`x-options-depends-on`)。后端两处都会写。 */
export function optionDependency(p: ExposedParam): OptionDependency | null {
  const c = (p.constraints ?? {}) as Record<string, unknown>
  const dependsOn = c.options_depends_on ?? c['x-options-depends-on']
  const source = c.options_source ?? c['x-options-source']
  if (typeof dependsOn !== 'string' || !dependsOn) return null
  if (typeof source !== 'string' || !source) return null
  return { dependsOn, source }
}

/** 注册时冻结下来的静态选项 —— 动态清单没到/拉失败时的兜底与离线展示。 */
export function staticOptions(p: ExposedParam): FieldOption[] {
  const c = (p.constraints ?? {}) as Record<string, unknown>
  const meta = c.option_meta ?? c['x-option-meta']
  if (Array.isArray(meta)) {
    return (meta as FieldOption[]).filter((m) => m && m.value !== undefined)
  }
  const enumValues = c.enum
  if (Array.isArray(enumValues)) return enumValues.map((v) => ({ value: String(v) }))
  return []
}

export function isFieldVisible(p: ExposedParam, values: Record<string, unknown>): boolean {
  const conditions = p.constraints?.visible_when as Record<string, boolean> | undefined
  return !conditions || Object.entries(conditions).every(([key, expected]) => values[key] === expected)
}
