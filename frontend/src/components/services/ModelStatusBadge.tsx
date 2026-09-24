import { useServiceModelStatus, MODEL_STATE_VIS } from '../../api/serviceModels'
import type { ServiceModelRef } from '../../api/services'

/**
 * 服务背后模型的**实时加载状态**(与授权状态 active/paused 是两回事)。
 *
 * 2026-09-23 用户问 API Key 详情里的 `active` 是什么 —— 那是授权状态,不反映模型是否
 * 加载:huihui 闲置 1 小时被 TTL 卸载后,那一行照样显示 active,调用却返回 503
 * model_not_ready(数据面不在请求路径上加载模型)。这里把模型状态单独标出来。
 *
 * 数据源 = useServiceModelStatus(共享 ['engines'] 查询,ws 实时推送),服务列表卡片与
 * API Key 详情共用本组件。
 * - 单模型服务:直接写状态「已加载 · GPU 1 / 未加载 / 加载中 / 加载失败」
 * - 多模型服务(工作流 / ComfyUI 桥引用多个组件):「模型 x/y」
 * - 没有模型引用:不渲染
 */
// 底色块(用户 2026-09-24:标签要有底色)。与状态点同色系的半透明底。
const STATE_BG: Record<string, string> = {
  loaded: 'rgba(52,199,89,0.14)',
  loading: 'rgba(245,158,11,0.15)',
  failed: 'rgba(239,68,68,0.14)',
  cold: 'rgba(148,163,184,0.14)',
}

export default function ModelStatusBadge({ models }: { models: ServiceModelRef[] | undefined }) {
  const { refs, total, loaded, loading, failed } = useServiceModelStatus(models)
  if (total === 0) return null

  // 全加载=绿 / 有加载中=黄 / 有失败=红 / 否则灰。
  const state =
    failed > 0 ? 'failed' : loading > 0 ? 'loading' : loaded === total ? 'loaded' : 'cold'
  const vis = MODEL_STATE_VIS[state]

  let label: string
  let title: string
  if (total === 1) {
    const r = refs[0]
    if (r.state === 'loaded') {
      label = r.gpu != null ? `已加载 · GPU ${r.gpu}` : '已加载'
      title = '模型已加载,可以直接调用'
    } else if (r.state === 'loading') {
      label = '加载中'
      title = '模型正在启动,完成前调用会返回 503 model_not_ready'
    } else if (r.state === 'failed') {
      label = '加载失败'
      title = r.detail ? `上次加载失败:${r.detail}` : '上次加载失败'
    } else {
      label = '未加载'
      title = '模型未加载:调用会返回 503 model_not_ready,需先在「模型」页加载(非常驻模型闲置超时会自动卸载)'
    }
  } else {
    label = `模型 ${loaded}/${total}`
    title = `模型 已加载 ${loaded}/${total}${loading ? ` · 加载中 ${loading}` : ''}${failed ? ` · 失败 ${failed}` : ''}`
  }

  return (
    <span
      title={title}
      style={{
        fontSize: 10,
        padding: '1px 7px',
        borderRadius: 10,
        background: STATE_BG[state],
        color: vis.color,
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        whiteSpace: 'nowrap',
      }}
    >
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: vis.color, flexShrink: 0 }} />
      {label}
    </span>
  )
}
