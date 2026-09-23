import { useState } from 'react'
import { X } from 'lucide-react'
import {
  useDeleteEngine,
  useDeletePreflight,
  type CodeRef,
  type DeletePreflight,
  type EngineInfo,
} from '../../api/engines'
import { copyText } from '../../utils/clipboard'

/**
 * 引擎库条目物理删除确认框(spec 2026-07-28-model-physical-delete)。
 *
 * 删除是 rm -rf 且不可撤销,所以这个框的职责是**把代价摊开给用户看**:目标路径、
 * 将释放的空间、会被清掉的注册表项、还引用它的服务、以及仓库里的源码残留引用。
 * 确认方式是键入模型名 —— 比"你确定吗"多一道手上的门槛。
 */
export default function DeleteModelDialog(
  { engine, onClose }: { engine: EngineInfo; onClose: () => void },
) {
  const { data, isLoading, isError } = useDeletePreflight(engine.name)

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="rounded-lg"
        style={{
          width: 560, maxWidth: '94vw', maxHeight: '86vh', overflowY: 'auto',
          background: 'var(--bg-elevated, #1a1a1a)', border: '1px solid var(--border)',
          padding: 20, display: 'flex', flexDirection: 'column', gap: 14,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: '#e55' }}>
            物理删除 · {engine.display_name}
          </div>
          <button
            onClick={onClose}
            aria-label="关闭"
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)' }}
          >
            <X size={16} />
          </button>
        </div>

        {isLoading && <div style={{ fontSize: 12, color: 'var(--muted)' }}>体检中…</div>}
        {isError && (
          <div style={{ fontSize: 12, color: '#e55' }}>预检失败,无法确认删除目标 —— 已中止。</div>
        )}
        {data && <DeleteBody engine={engine} pre={data} onClose={onClose} />}
      </div>
    </div>
  )
}

function fmtBytes(n: number): string {
  const gb = n / 1024 ** 3
  if (gb >= 0.1) return `${gb.toFixed(1)} GB`
  return `${Math.round(n / 1024 ** 2)} MB`
}

function buildCleanupPrompt(engine: EngineInfo, pre: DeletePreflight): string {
  const reg: string[] = []
  if (pre.registry_cleanup.models_d_yaml) reg.push(pre.registry_cleanup.models_d_yaml)
  if (pre.registry_cleanup.model_metadata) reg.push('DB model_metadata 行')
  if (pre.registry_cleanup.runtime_overrides > 0) {
    reg.push(`DB model_runtime_overrides ${pre.registry_cleanup.runtime_overrides} 行`)
  }
  const refs = pre.code_refs.map((r: CodeRef) => `- ${r.file}:${r.line}  ${r.text}`).join('\n')
  return [
    `已从 nous-engine 物理删除模型 \`${engine.name}\``,
    `(目标 ${pre.target_path},释放 ${fmtBytes(pre.size_bytes)})。`,
    reg.length > 0 ? `注册表已清理:${reg.join('、')}。` : '该条目无注册表项需清理。',
    '',
    '以下源文件仍引用它,请逐个判断并清理(不确定的先报给我,不要盲删):',
    refs,
    pre.code_refs_truncated ? '\n(结果已截断,清完请重新扫描确认还有没有残留。)' : '',
  ].join('\n')
}

function DeleteBody(
  { engine, pre, onClose }:
  { engine: EngineInfo; pre: DeletePreflight; onClose: () => void },
) {
  const del = useDeleteEngine()
  const [typed, setTyped] = useState('')
  const [ack, setAck] = useState(false)
  // copyText 两路(clipboard API / execCommand)都失败时的最后降级:展开只读 textarea
  // 让用户自己全选复制。
  const [fallbackText, setFallbackText] = useState<string | null>(null)

  const blocked = pre.blockers.loaded
  const services = pre.blockers.services
  const nameMatches = typed === engine.display_name
  const canDelete = nameMatches && (services.length === 0 || ack)

  const onCopyPrompt = () => {
    const text = buildCleanupPrompt(engine, pre)
    void copyText(text).then((ok) => {
      if (!ok) setFallbackText(text)
    })
  }

  if (blocked) {
    return (
      <div style={{ fontSize: 12, color: 'var(--text)', lineHeight: 1.7 }}>
        <div style={{ color: '#e55', fontWeight: 600, marginBottom: 6 }}>
          该模型正在显存中（{blocked.status}
          {blocked.gpu != null ? ` · GPU ${blocked.gpu}` : ''}），请先卸载再删除。
        </div>
        <div style={{ color: 'var(--muted)' }}>
          删除仍被 mmap 的权重文件不会立刻报错，但磁盘不会真释放，之后重新加载必然失败。
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 12, lineHeight: 1.7 }}>
      <Row label="目标">
        <code style={{ wordBreak: 'break-all' }}>{pre.target_path}</code>
        <div style={{ color: 'var(--muted)' }}>
          {pre.is_dir ? '整个目录' : '单文件'} · 将释放 <b style={{ color: '#e55' }}>{fmtBytes(pre.size_bytes)}</b>
        </div>
      </Row>

      <Row label="注册表清理">
        {pre.registry_cleanup.models_d_yaml || pre.registry_cleanup.model_metadata
          || pre.registry_cleanup.runtime_overrides > 0 ? (
          <ul style={{ margin: 0, paddingLeft: 16, color: 'var(--muted)' }}>
            {pre.registry_cleanup.models_d_yaml && <li>{pre.registry_cleanup.models_d_yaml}</li>}
            {pre.registry_cleanup.model_metadata && <li>DB model_metadata 行</li>}
            {pre.registry_cleanup.runtime_overrides > 0 && (
              <li>DB model_runtime_overrides {pre.registry_cleanup.runtime_overrides} 行</li>
            )}
          </ul>
        ) : (
          <span style={{ color: 'var(--muted)' }}>无（该条目不在注册表里）</span>
        )}
      </Row>

      {services.length > 0 && (
        <Row label="被服务引用">
          <ul style={{ margin: 0, paddingLeft: 16, color: '#d90' }}>
            {services.map((s) => <li key={s.id}>{s.name}</li>)}
          </ul>
          <label style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 6, cursor: 'pointer' }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            <span>我知道这些服务将指向不存在的模型</span>
          </label>
        </Row>
      )}

      <Row label="代码残留引用">
        {pre.code_refs_error && (
          <div style={{ color: '#d90' }}>扫描失败：{pre.code_refs_error}（不影响删除）</div>
        )}
        {pre.code_refs.length === 0 ? (
          <span style={{ color: 'var(--muted)' }}>无</span>
        ) : (
          <>
            <details>
              <summary style={{ cursor: 'pointer', color: 'var(--muted)' }}>
                {pre.code_refs.length} 处{pre.code_refs_truncated ? '（已截断）' : ''}
              </summary>
              <ul style={{ margin: '6px 0 0', paddingLeft: 16, color: 'var(--muted)' }}>
                {pre.code_refs.map((r, i) => (
                  <li key={`${r.file}:${r.line}:${i}`}>
                    <code>{r.file}:{r.line}</code>
                  </li>
                ))}
              </ul>
            </details>
            <div style={{ color: 'var(--muted)', marginTop: 6 }}>
              源码不会被自动修改 —— 复制下面的 prompt 交给 coding agent 处理。
            </div>
            <button
              onClick={onCopyPrompt}
              style={{
                marginTop: 6, padding: '4px 10px', fontSize: 12, cursor: 'pointer',
                background: 'transparent', color: 'var(--text)',
                border: '1px solid var(--border)', borderRadius: 4,
              }}
            >
              复制清理 prompt
            </button>
            {fallbackText !== null && (
              <textarea
                readOnly
                aria-label="清理 prompt"
                value={fallbackText}
                onFocus={(e) => e.currentTarget.select()}
                style={{
                  width: '100%', height: 140, marginTop: 6, fontSize: 11,
                  fontFamily: 'monospace', background: 'var(--bg)', color: 'var(--text)',
                  border: '1px solid var(--border)', borderRadius: 4, padding: 6,
                }}
              />
            )}
          </>
        )}
      </Row>

      <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
        <div style={{ marginBottom: 6 }}>
          删除不可撤销。键入 <b>{engine.display_name}</b> 以确认：
        </div>
        <input
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={engine.display_name}
          style={{
            width: '100%', padding: '6px 8px', fontSize: 12,
            background: 'var(--bg)', color: 'var(--text)',
            border: '1px solid var(--border)', borderRadius: 4,
          }}
        />
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button
          onClick={onClose}
          style={{
            padding: '6px 14px', fontSize: 12, cursor: 'pointer', background: 'transparent',
            color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 4,
          }}
        >
          取消
        </button>
        <button
          disabled={!canDelete || del.isPending}
          onClick={() =>
            del.mutate(
              { name: engine.name, force: services.length > 0 },
              { onSuccess: onClose },
            )
          }
          style={{
            padding: '6px 14px', fontSize: 12, borderRadius: 4, border: 'none',
            background: canDelete ? '#c33' : 'var(--border)',
            color: canDelete ? '#fff' : 'var(--muted)',
            cursor: canDelete && !del.isPending ? 'pointer' : 'not-allowed',
          }}
        >
          {del.isPending ? '删除中…' : '永久删除'}
        </button>
      </div>
    </div>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ color: 'var(--muted)', fontSize: 11, marginBottom: 2 }}>{label}</div>
      {children}
    </div>
  )
}
