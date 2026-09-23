import type { ServiceCapabilities } from '../../api/services'

/** tokens → K:262144 → 256K,32768 → 32K;不整除保留一位小数,< 1024 原样。 */
export function formatContextK(tokens: number): string {
  if (tokens < 1024) return String(tokens)
  const k = tokens / 1024
  return Number.isInteger(k) ? `${k}K` : `${k.toFixed(1)}K`
}

export const THINKING_HINT = '默认开启,思考内容在 reasoning_content 字段'
export const OUTPUT_HINT = '无单独上限,≤ 上下文 − 输入'

const yesNo = (b: boolean) => (b ? '是' : '否')

/** 「复制客户端配置」的多行纯文本 —— 对着第三方客户端的「供应商」表单逐项抄。 */
export function buildClientConfigText(opts: {
  url: string
  model: string
  caps: ServiceCapabilities
}): string {
  const { url, model, caps } = opts
  const ctx = caps.context != null ? formatContextK(caps.context) : '未设置(引擎自动)'
  const out = caps.max_output != null ? formatContextK(caps.max_output) : '使用默认(≤ 上下文)'
  return [
    `接口地址: ${url}`,
    `模型名称: ${model}`,
    `工具调用: ${yesNo(caps.tools)}   图片输入: ${yesNo(caps.vision)}   思考模式: ${yesNo(caps.thinking)}`,
    `输入(上下文): ${ctx}   输出: ${out}`,
    `提供商: ${caps.provider ?? '未知'}`,
  ].join('\n')
}
