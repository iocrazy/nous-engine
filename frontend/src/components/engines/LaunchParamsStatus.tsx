/**
 * 「启动参数」弹窗顶部的状态区:上次加载失败的原因 + 哪些参数被覆盖了(与 yaml 默认对照)。
 *
 * 2026-09-22 huihui 被点成 256K 预设,下次加载起不来,但弹窗里什么都看不出:既不知道
 * 失败了、为什么失败,也不知道它原本是 32K、该退回哪里。原因只藏在模型卡片状态的
 * 悬停提示里,而且当时还是被截掉的包装异常。
 */

function fmt(v: unknown): string {
  if (typeof v === 'number' && v >= 1024 && v % 1024 === 0) return `${v}(${v / 1024}K)`
  if (typeof v === 'boolean') return v ? '开' : '关'
  return v == null ? '未设置' : String(v)
}

export function LaunchParamsStatus({
  status,
  statusDetail,
  overridden,
  effective,
  defaults,
}: {
  status: string
  statusDetail: string | null | undefined
  overridden: string[]
  effective: Record<string, unknown>
  defaults: Record<string, unknown>
}) {
  const failed = status === 'failed' && !!statusDetail
  if (!failed && overridden.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {failed && (
        <div
          role="alert"
          style={{
            fontSize: 12, lineHeight: 1.6, padding: '8px 10px', borderRadius: 6,
            border: '1px solid var(--danger, #ef4444)', color: 'var(--danger, #ef4444)',
            background: 'rgba(239,68,68,0.08)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 2 }}>上次加载失败</div>
          {statusDetail}
          {overridden.length > 0 && (
            <div style={{ marginTop: 4, color: 'var(--muted)' }}>
              如果是改参数之后才起不来,点下面对应项的「恢复默认」再重新加载。
            </div>
          )}
        </div>
      )}
      {overridden.length > 0 && (
        <div style={{ fontSize: 11, color: 'var(--warn)', lineHeight: 1.7 }}>
          {overridden.map((k) => (
            <div key={k}>
              已覆盖 <code>{k}</code> = {fmt(effective[k])}(yaml 默认 {fmt(defaults[k])})
            </div>
          ))}
          <div style={{ color: 'var(--muted)' }}>其余参数为 models.d 的 yaml 默认。</div>
        </div>
      )}
    </div>
  )
}
