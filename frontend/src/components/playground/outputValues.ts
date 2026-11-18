import { paramKey, paramSlot, type ExposedParam } from '../../api/services'

/**
 * Common slot names a node might emit its primary value on. We try the
 * declared `input_name` first, then fall back through these so the publish
 * default `input_name='value'` still resolves against nodes that actually
 * emit `text` / `output` / `content` / etc.
 */
const SLOT_FALLBACKS = ['text', 'value', 'output', 'content', 'result', 'data'] as const

export function pluck(
  result: Record<string, unknown>,
  param: ExposedParam,
): unknown {
  // Backend wraps node outputs as `{ outputs: { node_id: { slot: value } } }`.
  // Older callers may pass the inner dict directly — accept both.
  const root = (result.outputs && typeof result.outputs === 'object'
    ? result.outputs
    : result) as Record<string, unknown>

  const nodeBucket = root[param.node_id]
  if (nodeBucket && typeof nodeBucket === 'object') {
    const bucket = nodeBucket as Record<string, unknown>
    const slot = paramSlot(param)
    if (slot && bucket[slot] !== undefined) return bucket[slot]
    for (const fb of SLOT_FALLBACKS) {
      if (bucket[fb] !== undefined) return bucket[fb]
    }
    // Single-key bucket — use the only value
    const keys = Object.keys(bucket)
    if (keys.length === 1) return bucket[keys[0]]
    return bucket
  }
  // Fallback: flat shape keyed by exposed key (legacy / direct caller)
  const k = paramKey(param)
  if (k && root[k] !== undefined) return root[k]
  return undefined
}
