import type { LaunchParamsBody } from '../../api/vllm'

// 常用上下文档位。256K = Qwen3.8 系列原生上限(max_position_embeddings 262144)。
export const CTX_PRESETS = [
  { label: '32K', value: 32768 },
  { label: '128K', value: 131072 },
  { label: '256K', value: 262144 },
]

export const NUMERIC_KEYS = [
  'max_model_len',
  'max_num_seqs',
  'max_num_batched_tokens',
] as const
export type NumericKey = (typeof NUMERIC_KEYS)[number]

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
