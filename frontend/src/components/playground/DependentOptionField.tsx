import { useEffect } from 'react'
import { useComfyStyles } from '../../api/comfyTemplates'
import NodeSelectPopover from '../nodes/NodeSelectPopover'
import OptionThumbGrid, { type OptionMeta } from './OptionThumbGrid'
import { parseValues, type FieldOption } from './fieldKind'

export interface DependentOptionFieldProps {
  /** 清单来源(`constraints.options_source`)。目前只有 `comfy_styles`。 */
  source: string
  /** 依赖参数的**当前值**,即拉清单用的入参(风格包名)。空 = 还没选,直接用静态清单。 */
  dependsValue: string
  /** 注册时冻结的静态清单 —— 加载中/失败时的兜底。 */
  fallback: FieldOption[]
  multiple: boolean
  topLayerSelect?: boolean
  selectLabel?: string
  value: unknown
  onChange: (v: unknown) => void
}

/**
 * 选项随另一个参数联动的字段。
 *
 * 为什么单独一个组件而不是在 `FieldInput` 里加分支:里面要 `useQuery`,而 hook 不能
 * 条件调用 —— 塞进 FieldInput 会让**所有**字段(以及 ComfyTemplateEditor 的节点预览、
 * 既有的一堆无 QueryClientProvider 的测试)都被迫依赖 React Query。做成只在真声明了
 * 依赖时才挂载的子组件,其余路径一行代码都不受影响。
 *
 * 「缩略图网格还是普通下拉」由**实际拿到的清单**有没有 image 决定,不由父组件按注册时
 * 冻结的静态数据算一次定死 —— 只声明依赖、没冻结静态 options 的 mapping 在父组件那边
 * 算出来永远是下拉,而运行期拉到的清单可能项项带图(krea2 的风格包就是)。
 */
export default function DependentOptionField({
  source,
  dependsValue,
  fallback,
  multiple,
  topLayerSelect,
  selectLabel,
  value,
  onChange,
}: DependentOptionFieldProps) {
  // 目前只有风格清单这一种来源;来源不认识就老老实实退回静态清单(而不是白屏)。
  const pack = source === 'comfy_styles' && dependsValue ? dependsValue : undefined
  const { data, isFetching, isError } = useComfyStyles(pack)

  const options: FieldOption[] = data?.options ?? fallback
  const current = String(value ?? '')
  // 任何一项带缩略图就渲网格(275 项风格用纯文本下拉没法挑);一张图都没有退回下拉。
  const useGrid = options.some((o) => o.image)

  // 切包后把**已选但新包里没有**的值清掉 —— 留着提交必被后端白名单拒(422),
  // 而且缩略图网格里也根本渲不出来(用户看不见自己选了什么)。
  // 只在清单真拿到(data 存在)时才剪,加载中/失败时不动用户已选的值。
  useEffect(() => {
    if (!data) return
    if (!current) return
    const allowed = new Set(data.options.map((o) => o.value))
    if (multiple) {
      const picked = parseValues(current)
      const kept = picked.filter((v) => allowed.has(v))
      if (kept.length !== picked.length) onChange(kept.join(','))
    } else if (!allowed.has(current)) {
      onChange('')
    }
  }, [data, current, multiple, onChange])

  const hint = isError
    ? `选项清单加载失败(ComfyUI 可能未运行),已回退到默认清单`
    : isFetching && !data
      ? '正在加载选项…'
      : ''

  return (
    <div>
      {hint && (
        <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6 }}>{hint}</div>
      )}
      {useGrid ? (
        <OptionThumbGrid
          options={options as OptionMeta[]}
          value={current}
          multiple={multiple}
          onChange={onChange}
        />
      ) : (
        <NodeSelectPopover
          value={current}
          onChange={onChange}
          options={options.map((o) => ({ value: o.value, label: o.label ?? o.value }))}
          placeholder="请选择"
          topLayer={topLayerSelect}
          appearance={topLayerSelect ? 'console' : undefined}
          ariaLabel={selectLabel}
        />
      )}
    </div>
  )
}
