import { useState } from 'react'
import { useUpdateLaunchParams, type LaunchParamsBody } from '../../api/vllm'

// 常用上下文档位。256K = Qwen3.8 系列原生上限(max_position_embeddings 262144)。
const CTX_PRESETS = [
  { label: '32K', value: 32768 },
  { label: '128K', value: 131072 },
  { label: '256K', value: 262144 },
]

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

  const num = (k: keyof LaunchParamsBody) =>
    (draft[k] as number | undefined) ?? (current[k] as number | undefined) ?? ''

  return (
    <div style={{ display: 'grid', gap: 10, fontSize: 12 }}>
      <div>
        <label style={{ display: 'block', marginBottom: 4 }}>
          上下文长度 (max_model_len)
        </label>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input
            type="number"
            value={num('max_model_len')}
            onChange={(e) =>
              setDraft({ ...draft, max_model_len: Number(e.target.value) })
            }
            style={{ width: 110 }}
          />
          {CTX_PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              disabled={update.isPending}
              onClick={() => save({ max_model_len: p.value })}
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
          value={num('max_num_seqs')}
          onChange={(e) =>
            setDraft({ ...draft, max_num_seqs: Number(e.target.value) })
          }
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
          value={num('max_num_batched_tokens')}
          onChange={(e) =>
            setDraft({
              ...draft,
              max_num_batched_tokens: Number(e.target.value),
            })
          }
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
          onChange={(e) => save({ enable_prefix_caching: e.target.checked })}
        />
        Prefix Caching(共享 system prompt 时跳过重复 prefill)
      </label>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button
          type="button"
          disabled={update.isPending || Object.keys(draft).length === 0}
          onClick={() => {
            save(draft)
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
