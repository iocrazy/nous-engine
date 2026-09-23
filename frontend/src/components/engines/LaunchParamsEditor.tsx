import { useState } from 'react'
import { useUpdateLaunchParams, type LaunchParamsBody } from '../../api/vllm'
// 常量与纯函数放同目录的 launchParams.ts:本文件只导出组件。
// 组件文件里再导出别的东西会让 Fast Refresh 失效(react-refresh/only-export-components,
// CI 的 lint 是 error 不是 warning)。
import { CTX_PRESETS, sanitizeLaunchParams, type NumericKey } from './launchParams'

/**
 * 「恢复默认」= 发 `{key: null}` 清除该覆盖、回退 models.d 的 yaml 值。
 * 三态的后端从 Task 2/4 起就支持,**前端一直没发过** —— 于是用户改了 max_model_len
 * 之后没有任何办法退回默认,只能再猜一个值填回去(2026-09-22 复查 N5)。
 *
 * ⚠️ **定义在模块顶层,不在 LaunchParamsEditor 的函数体里**:在 render 期间定义组件,
 * 每次渲染都是一个新的组件类型,React 会卸载重建整棵子树(状态丢失 + 闪烁);
 * eslint 的 `Cannot create components during render` 就是 error 级别拦这个。
 */
function ResetButton({
  k,
  overridden,
  disabled,
  onReset,
}: {
  k: keyof LaunchParamsBody
  overridden: string[]
  disabled: boolean
  onReset: (k: keyof LaunchParamsBody) => void
}) {
  if (!overridden.includes(k)) return null
  return (
    <button
      type="button"
      aria-label={`恢复默认 ${k}`}
      disabled={disabled}
      onClick={() => onReset(k)}
      style={{ fontSize: 11, color: 'var(--muted)' }}
    >
      恢复默认
    </button>
  )
}

export function LaunchParamsEditor({
  engineName,
  current,
  editable,
  overridden,
}: {
  engineName: string
  current: Record<string, unknown>
  /**
   * 该引擎的适配器**真正消费得了**的键(后端 GET 的 `editable`)。不在里面的控件
   * 一律不渲染 —— 渲染了就是"点了不管用的按钮":写得进库、GET 报「已覆盖」、
   * 引擎行为纹丝不动(MOSS ASR / TTS 的适配器用 `**kwargs` 收尾)。
   */
  editable: string[]
  /** 哪几个键当前被运行时覆盖 —— 只有它们需要「恢复默认」。 */
  overridden: string[]
}) {
  const update = useUpdateLaunchParams()
  const [draft, setDraft] = useState<LaunchParamsBody>({})

  const save = (patch: LaunchParamsBody) =>
    update.mutate({ name: engineName, body: patch })

  /**
   * 即时保存(预设按钮 / 复选框)之后必须把对应的键从 draft 里**删掉**,回落到刷新后的
   * `current`。否则:用户在输入框敲了个值(进 draft)→ 点 256K 预设(发请求,draft 里
   * 还是旧值)→ 点"保存"(把 draft 的旧值发出去)→ 刚存的 256K 被悄悄覆盖。
   */
  const saveAndDropDraft = (patch: LaunchParamsBody) => {
    save(patch)
    setDraft((d) => {
      const next = { ...d }
      for (const k of Object.keys(patch) as (keyof LaunchParamsBody)[]) delete next[k]
      return next
    })
  }

  // 空输入框 → 从 draft 里删掉该键(回落 current),**不是** Number('') === 0。
  const onNumChange = (k: NumericKey) => (v: string) =>
    setDraft((d) => {
      const next = { ...d }
      if (v.trim() === '') delete next[k]
      else next[k] = Number(v)
      return next
    })

  const num = (k: NumericKey) =>
    (draft[k] as number | undefined) ?? (current[k] as number | undefined) ?? ''

  const can = (k: keyof LaunchParamsBody) => editable.includes(k)

  const onReset = (k: keyof LaunchParamsBody) =>
    saveAndDropDraft({ [k]: null } as LaunchParamsBody)
  // 四个 ResetButton 的公共入参,免得每处重复三行。
  const resetProps = { overridden, disabled: update.isPending, onReset }

  const payload = sanitizeLaunchParams(draft)
  const canSave = Object.keys(payload).length > 0

  return (
    <div style={{ display: 'grid', gap: 10, fontSize: 12 }}>
      {can('max_model_len') && (
      <div>
        <label style={{ display: 'block', marginBottom: 4 }}>
          上下文长度 (max_model_len)
        </label>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input
            type="number"
            aria-label="max_model_len"
            value={num('max_model_len')}
            onChange={(e) => onNumChange('max_model_len')(e.target.value)}
            style={{ width: 110 }}
          />
          {CTX_PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              disabled={update.isPending}
              onClick={() => saveAndDropDraft({ max_model_len: p.value })}
            >
              {p.label}
            </button>
          ))}
          <ResetButton k="max_model_len" {...resetProps} />
        </div>
        <p style={{ color: 'var(--muted)', marginTop: 4 }}>
          上下文与并发此消彼长:同样的 KV 池,长度减半则并发翻倍。
          调大可能因 KV 装不下而<strong>起不来</strong>(vLLM 启动时会拒绝),失败原因会显示在本窗口顶部。
        </p>
      </div>
      )}

      {can('max_num_seqs') && (
      <>
      {/* 「恢复默认」一律放在 <label> **外面**:label 内的点击会转发给被标注的控件,
          放进去等于点一下既清覆盖又顺手改了那个控件的值。 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <label>
          最大并发序列 (max_num_seqs)
          <input
            type="number"
            aria-label="max_num_seqs"
            value={num('max_num_seqs')}
            onChange={(e) => onNumChange('max_num_seqs')(e.target.value)}
            style={{ width: 90, marginLeft: 6 }}
          />
        </label>
        <ResetButton k="max_num_seqs" {...resetProps} />
      </div>
      <p style={{ color: 'var(--muted)', marginTop: -6 }}>
        这是<strong>调度上限,不是并发驱动力</strong> —— 真实并发由 KV 池决定,只调大它不给 KV 没有提升。
      </p>
      </>
      )}

      {can('max_num_batched_tokens') && (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <label>
          单批最大 token (max_num_batched_tokens)
          <input
            type="number"
            aria-label="max_num_batched_tokens"
            value={num('max_num_batched_tokens')}
            onChange={(e) => onNumChange('max_num_batched_tokens')(e.target.value)}
            style={{ width: 90, marginLeft: 6 }}
          />
        </label>
        <ResetButton k="max_num_batched_tokens" {...resetProps} />
      </div>
      )}

      {can('enable_prefix_caching') && (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <input
            type="checkbox"
            checked={Boolean(
              draft.enable_prefix_caching ?? current.enable_prefix_caching,
            )}
            disabled={update.isPending}
            onChange={(e) =>
              saveAndDropDraft({ enable_prefix_caching: e.target.checked })
            }
          />
          Prefix Caching(共享 system prompt 时跳过重复 prefill)
        </label>
        <ResetButton k="enable_prefix_caching" {...resetProps} />
      </div>
      )}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button
          type="button"
          disabled={update.isPending || !canSave}
          onClick={() => {
            save(payload)
            setDraft({})
          }}
        >
          保存
        </button>
        <span style={{ color: 'var(--muted)' }}>
          {update.isPending
            ? '保存中…'
            : '已保存的改动需 unload + load 才生效;起不来时失败原因会显示在本窗口顶部'}
        </span>
      </div>

      <p style={{ color: 'var(--muted)', borderTop: '1px solid var(--border)', paddingTop: 8 }}>
        显存预算不在这里 —— 走「显存预算」那栏(绝对 GiB)。
        <code>gpu_memory_utilization</code> 是「占该卡总量」的比例,换卡必须重算,
        所以不作为可编辑项。
      </p>
    </div>
  )
}
