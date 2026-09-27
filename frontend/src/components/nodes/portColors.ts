// 端口类型 → 颜色映射(单一真相来源)。端口圆点(BaseNode 的 <Handle>)与连接 edge
// (PortTypedEdge)共用,所以抽成独立模块 —— 放 BaseNode.tsx 里 export 会触发
// react-refresh/only-export-components(组件文件只该导出组件)。
//
// 小写(text/audio/image…)= Family A 内置端口;大写(MODEL/CLIP/VAE/CONDITIONING/
// LATENT)= flux2-components 细粒度图端口(走 plugin defs 字符串),配色对齐 ComfyUI
// 调色板,迁移工作流的用户不必重学哪个口是哪个。
export const PORT_TYPE_COLORS: Record<string, string> = {
  text: 'var(--ok)',
  audio: 'var(--info)',
  control: 'var(--accent)',
  any: 'var(--purple)',
  image: 'rgba(20,184,166,0.85)', // teal-cyan
  MODEL: 'rgba(244,114,182,0.9)', // pink
  CLIP: 'rgba(234,179,8,0.9)', // yellow
  VAE: 'rgba(239,68,68,0.85)', // red
  CONDITIONING: 'rgba(251,146,60,0.9)', // orange
  LATENT: 'rgba(168,85,247,0.85)', // purple
  // 留噪 latent 接力(PR-B2):VAE Decode(output_mode=latent)→ KSampler init_latent。区别于
  // LATENT(采样计划描述符)—— LATENT_REF 是落盘的真 latent 张量引用,用更亮的靛蓝区分。
  LATENT_REF: 'rgba(129,140,248,0.9)', // indigo
  // LCS 采样期干预描述符(锐化/保色,接 KSampler interventions 端口):琥珀色。
  intervene: 'rgba(245,158,11,0.9)', // amber
}
