import { useMemo } from 'react'
import { useEngines, type EngineInfo } from './engines'
import { useAllComponentStates, loadedStateByFile, type ComponentLoadState } from './components'
import type { ServiceModelRef } from './services'

export type ModelLoadState = ComponentLoadState // 'cold' | 'loading' | 'loaded' | 'failed'

export interface ResolvedModelRef extends ServiceModelRef {
  state: ModelLoadState
  detail: string | null
  /** 已加载时实际落的卡(引擎类);组件 / 未加载 → null */
  gpu: number | null
}

export interface ServiceModelStatus {
  refs: ResolvedModelRef[]
  total: number
  loaded: number
  loading: number
  failed: number
}

function engineToState(e: EngineInfo | undefined): ModelLoadState {
  if (!e) return 'cold'
  if (e.status === 'loaded') return 'loaded'
  if (e.status === 'loading') return 'loading'
  if (e.status === 'failed') return 'failed'
  return 'cold' // unloaded
}

/** Overlay live load-state onto a service's static model refs.
 *  - component refs → matched by file against the component-state registry
 *  - engine refs    → matched by engine_key against /api/v1/engines name
 *  Shares the global ['engines'] + ['component-states-all'] queries (both
 *  ws-driven), so many cards mounting this stay cheap + live. */
export function useServiceModelStatus(models: ServiceModelRef[] | undefined): ServiceModelStatus {
  const { data: engines } = useEngines()
  const { data: compStates } = useAllComponentStates()

  return useMemo(() => {
    const byFile = loadedStateByFile(compStates)
    const engineByName = new Map((engines ?? []).map((e) => [e.name, e]))
    const refs: ResolvedModelRef[] = (models ?? []).map((m) => {
      if (m.kind === 'component') {
        const state = (m.file && byFile[m.file]) || 'cold'
        return { ...m, state, detail: null, gpu: null }
      }
      const e = m.engine_key ? engineByName.get(m.engine_key) : undefined
      return { ...m, state: engineToState(e), detail: e?.status_detail ?? null, gpu: e?.loaded_gpu ?? null }
    })
    return {
      refs,
      total: refs.length,
      loaded: refs.filter((r) => r.state === 'loaded').length,
      loading: refs.filter((r) => r.state === 'loading').length,
      failed: refs.filter((r) => r.state === 'failed').length,
    }
  }, [models, engines, compStates])
}

export const MODEL_STATE_VIS: Record<ModelLoadState, { label: string; color: string }> = {
  loaded: { label: '已加载', color: 'var(--ok, #34c759)' },
  loading: { label: '加载中', color: 'var(--warn, #f59e0b)' },
  failed: { label: '失败', color: 'var(--error, #ef4444)' },
  cold: { label: '未加载', color: 'var(--muted)' },
}

export const MODEL_ROLE_LABEL: Record<string, string> = {
  diffusion_models: '扩散模型',
  clip: 'CLIP',
  vae: 'VAE',
  checkpoint: 'Checkpoint',
  llm: 'LLM',
  tts: 'TTS',
}
