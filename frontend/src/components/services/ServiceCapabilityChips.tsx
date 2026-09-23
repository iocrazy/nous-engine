import type { ServiceCapabilities } from '../../api/services'
import { OUTPUT_HINT, THINKING_HINT, formatContextK } from './serviceCapabilities'

const chipStyle: React.CSSProperties = {
  fontSize: 10,
  padding: '1px 7px',
  borderRadius: 10,
  background: 'var(--bg)',
  color: 'var(--muted)',
  whiteSpace: 'nowrap',
}

/**
 * 服务能力小标签:`256K 上下文` · `工具` · `思考` · `图片` · `huihui-ai`,没有的项不显示。
 * 给用户往第三方客户端「供应商」配置里抄值用;无 capabilities(非 model 服务)不渲染。
 */
export default function ServiceCapabilityChips({
  capabilities: caps,
}: {
  capabilities?: ServiceCapabilities | null
}) {
  if (!caps) return null
  const chips: { key: string; label: string; title: string }[] = []
  if (caps.context != null) {
    chips.push({
      key: 'context',
      label: `${formatContextK(caps.context)} 上下文`,
      title: `上下文 ${caps.context.toLocaleString()} tokens;输出${OUTPUT_HINT}`,
    })
  }
  if (caps.tools) chips.push({ key: 'tools', label: '工具', title: '支持工具调用(tools / tool_choice)' })
  if (caps.thinking) chips.push({ key: 'thinking', label: '思考', title: `思考模式:${THINKING_HINT}` })
  if (caps.vision) chips.push({ key: 'vision', label: '图片', title: '支持图片输入' })
  if (caps.provider) {
    chips.push({ key: 'provider', label: caps.provider, title: `提供商(上游仓库 ${caps.source ?? caps.provider})` })
  }
  if (!chips.length) return null
  return (
    <div data-testid="service-capability-chips" style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
      {chips.map((c) => (
        <span key={c.key} title={c.title} style={chipStyle}>
          {c.label}
        </span>
      ))}
    </div>
  )
}
