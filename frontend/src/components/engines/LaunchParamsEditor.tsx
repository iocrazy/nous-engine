import { useState } from 'react'
import { useUpdateLaunchParams, type LaunchParamsBody } from '../../api/vllm'

// 常用上下文档位。256K = Qwen3.8 系列原生上限(max_position_embeddings 262144)。
const CTX_PRESETS = [
  { label: '32K', value: 32768 },
  { label: '128K', value: 131072 },
  { label: '256K', value: 262144 },
]

const NUMERIC_KEYS = [
  'max_model_len',
  'max_num_seqs',
  'max_num_batched_tokens',
] as const
type NumericKey = (typeof NUMERIC_KEYS)[number]

/**
 * 保存前过滤 draft:数字键只放行正整数。
 *
 * `Number('')` 是 **0** —— 清空输入框会把 0 存进 draft,点保存就写进 DB,而 0 让 vLLM
 * 起不来;更糟的是之后 `num()` 一直读到这个 0,用户在 UI 上再也退不出来,得去 DB 里捞。
 * 后端也有一道值域校验(400),这里是"根本别发出去"的那道。
 */
export function sanitizeLaunchParams(draft: LaunchParamsBody): LaunchParamsBody {
  const out: LaunchParamsBody = { ...draft }
  for (const k of NUMERIC_KEYS) {
    const v = out[k]
    if (typeof v !== 'number' || !Number.isInteger(v) || v <= 0) delete out[k]
  }
  return out
}

export function LaunchParamsEditor({
  engineName,
  current,
}: {
  engineName: string
  current: Record<string, unknown>
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

  const payload = sanitizeLaunchParams(draft)
  const canSave = Object.keys(payload).length > 0

  return (
    <div style={{ display: 'grid', gap: 10, fontSize: 12 }}>
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
        </div>
        <p style={{ color: 'var(--muted)', marginTop: 4 }}>
          上下文与并发此消彼长:同样的 KV 池,长度减半则并发翻倍。
          调大可能因 KV 装不下而**起不来**(vLLM 启动时会拒绝)。
        </p>
      </div>

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
      <p style={{ color: 'var(--muted)', marginTop: -6 }}>
        这是**调度上限,不是并发驱动力** —— 真实并发由 KV 池决定,只调大它不给 KV 没有提升。
      </p>

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
            : '改后需 unload + load 才生效'}
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
