import { useEffect, useRef, useState } from 'react'
import { Upload, X } from 'lucide-react'
import { createComfyTemplate, getObjectInfo } from '../../api/comfyTemplates'
import { NAME_RE, NAME_RULE_HINT } from '../../api/services'
import type { ComfyWorkflow } from './comfyGraphLayout'
import { applyModelFixes, findInvalidModelRefs, issueKey, type ModelRefFix, type ModelRefIssue } from './workflowModelCheck'

export interface ImportComfyDialogProps {
  open: boolean
  onClose: () => void
  /** Called with the newly created service's name on success. */
  onImported: (serviceName: string) => void
}

const INVALID_FORMAT_MSG =
  '不是 ComfyUI API 格式导出，请在 ComfyUI 用 Export (API) 导出'

export default function ImportComfyDialog({ open, onClose, onImported }: ImportComfyDialogProps) {
  const [fileName, setFileName] = useState('')
  const [workflow, setWorkflow] = useState<ComfyWorkflow | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  // 模型名自愈(见 workflowModelCheck.ts 头注 incident):提交前先拿 object_info 比对
  // combo/enum 字段,有不合法引用就先停在「review」步给用户挑正确值,不直接把坏 JSON
  // 送给后端(ComfyUI 执行时才报错,只能靠 shell 改 JSON,这就是要防的事故)。
  const [checkingModels, setCheckingModels] = useState(false)
  const [modelIssues, setModelIssues] = useState<ModelRefIssue[]>([])
  const [choices, setChoices] = useState<Record<string, string>>({})
  const [reviewMode, setReviewMode] = useState(false)
  const [modelCheckWarning, setModelCheckWarning] = useState<string | null>(null)
  const dropRef = useRef<HTMLLabelElement>(null)
  // FileReader in jsdom (and real browsers) resolves asynchronously; a click on
  // 导入 can land before parsing finishes, so submit() awaits this instead of
  // trusting the `workflow` state snapshot at click time.
  const parseRef = useRef<Promise<ComfyWorkflow | null>>(Promise.resolve(null))
  // Monotonic token so a stale FileReader callback (superseded by picking another
  // file, or still in flight when the dialog closes/reopens) can't clobber state
  // that belongs to a newer selection — the closure captures its own token and
  // compares against the current one before calling any setState.
  const readTokenRef = useRef(0)

  const resetModelCheck = () => {
    setCheckingModels(false)
    setModelIssues([])
    setChoices({})
    setReviewMode(false)
    setModelCheckWarning(null)
  }

  useEffect(() => {
    if (open) return
    readTokenRef.current += 1
    setFileName('')
    setWorkflow(null)
    setFileError(null)
    setName('')
    setSubmitting(false)
    setSubmitError(null)
    resetModelCheck()
    parseRef.current = Promise.resolve(null)
  }, [open])

  if (!open) return null

  const handleFile = (file: File) => {
    const token = (readTokenRef.current += 1)
    setFileName(file.name)
    setWorkflow(null)
    setFileError(null)
    setSubmitError(null)
    resetModelCheck()
    parseRef.current = new Promise((resolve) => {
      const reader = new FileReader()
      reader.onerror = () => {
        if (readTokenRef.current === token) setFileError('读取文件失败')
        resolve(null)
      }
      reader.onload = () => {
        let parsed: unknown
        try {
          parsed = JSON.parse(String(reader.result))
        } catch {
          if (readTokenRef.current === token) setFileError(INVALID_FORMAT_MSG)
          resolve(null)
          return
        }
        if (!isApiFormatWorkflow(parsed)) {
          if (readTokenRef.current === token) setFileError(INVALID_FORMAT_MSG)
          resolve(null)
          return
        }
        if (readTokenRef.current === token) setWorkflow(parsed)
        resolve(parsed)
      }
      reader.readAsText(file)
    })
  }

  const onFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) handleFile(file)
  }

  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (dropRef.current) dropRef.current.style.borderColor = 'var(--accent)'
  }

  const onDragLeave = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (dropRef.current) dropRef.current.style.borderColor = 'var(--border)'
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (dropRef.current) dropRef.current.style.borderColor = 'var(--border)'
    const file = e.dataTransfer.files?.[0]
    if (file) handleFile(file)
  }

  const nameValid = NAME_RE.test(name)

  const doImport = async (finalWorkflow: Record<string, unknown>) => {
    setSubmitting(true)
    setSubmitError(null)
    try {
      const res = await createComfyTemplate(name, finalWorkflow)
      onImported(res.service_name)
      onClose()
    } catch (e) {
      setSubmitError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  // 提交前先拿 object_info 比对模型名(见 workflowModelCheck.ts 头注的事故背景)。有不
  // 合法引用 → 切进 review 步等用户挑值,不直接放行;sidecar 离线(getObjectInfo 失败)
  // → 跳过校验,提示一句,照常导入,不能因为校验本身不可用就把导入功能一起挡住。
  const submit = async () => {
    if (submitting || checkingModels) return
    const wf = workflow ?? (await parseRef.current)
    if (!wf) {
      setSubmitError('请先选择合法的 ComfyUI API 格式工作流文件')
      return
    }
    if (!nameValid) return

    setCheckingModels(true)
    try {
      const info = await getObjectInfo()
      const issues = findInvalidModelRefs(wf, info)
      setCheckingModels(false)
      if (issues.length > 0) {
        setModelIssues(issues)
        const init: Record<string, string> = {}
        for (const iss of issues) if (iss.suggestion) init[issueKey(iss)] = iss.suggestion
        setChoices(init)
        setModelCheckWarning(null)
        setReviewMode(true)
        return
      }
      setModelCheckWarning(null)
    } catch {
      setCheckingModels(false)
      setModelCheckWarning('sidecar 离线，已跳过模型名校验')
    }
    await doImport(wf)
  }

  const confirmReview = async () => {
    if (submitting) return
    const wf = workflow ?? (await parseRef.current)
    if (!wf) return
    const fixes: ModelRefFix[] = modelIssues
      .map((iss) => ({ nodeId: iss.nodeId, inputName: iss.inputName, newValue: choices[issueKey(iss)] }))
      .filter((f): f is ModelRefFix => !!f.newValue)
    await doImport(applyModelFixes(wf, fixes))
  }

  const acceptAllSuggestions = () => {
    setChoices((prev) => {
      const next = { ...prev }
      for (const iss of modelIssues) if (iss.suggestion) next[issueKey(iss)] = iss.suggestion
      return next
    })
  }

  const hasSuggestions = modelIssues.some((iss) => !!iss.suggestion)
  const reviewIncomplete = modelIssues.some((iss) => !choices[issueKey(iss)])

  return (
    <div onClick={onClose} style={overlayStyle}>
      <div onClick={(e) => e.stopPropagation()} style={reviewMode ? { ...modalStyle, width: 560 } : modalStyle}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 16 }}>
          <h2 style={{ flex: 1, fontSize: 16, fontWeight: 600, color: 'var(--text)' }}>
            {reviewMode ? '模型引用有问题，先修正' : '导入 ComfyUI 工作流'}
          </h2>
          <button
            onClick={onClose}
            type="button"
            style={{ background: 'transparent', border: 'none', color: 'var(--muted)', cursor: 'pointer' }}
          >
            <X size={18} />
          </button>
        </div>

        {reviewMode ? (
          <>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
              以下 {modelIssues.length} 个字段的值不在本机模型库里(可能是别的机器导出的路径/文件名)，
              为每个选一个本机存在的值再导入。
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxHeight: '52vh', overflow: 'auto' }}>
              {modelIssues.map((iss) => {
                const key = issueKey(iss)
                return (
                  <div key={key} data-testid={`model-issue-${key}`} style={issueRowStyle}>
                    <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
                      <code style={{ fontFamily: 'var(--mono, monospace)' }}>{iss.classType}</code> #{iss.nodeId} ·{' '}
                      {iss.inputName}
                    </div>
                    <div style={{ fontSize: 11.5, color: 'var(--error, #ef4444)', marginBottom: 6, wordBreak: 'break-all' }}>
                      当前值不存在: "{iss.value}"
                    </div>
                    <select
                      aria-label={`选择 ${iss.inputName} 的正确值`}
                      value={choices[key] ?? ''}
                      onChange={(e) => setChoices((prev) => ({ ...prev, [key]: e.target.value }))}
                      style={inputStyle}
                    >
                      <option value="" disabled>
                        请选择…
                      </option>
                      {iss.options.map((o) => (
                        <option key={o} value={o}>
                          {o}
                          {o === iss.suggestion ? '（建议）' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                )
              })}
            </div>

            {submitError && <ErrorText>{submitError}</ErrorText>}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', alignItems: 'center', marginTop: 12 }}>
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={() => setReviewMode(false)} type="button" style={btnGhost}>
                  返回
                </button>
                {hasSuggestions && (
                  <button onClick={acceptAllSuggestions} type="button" style={btnGhost}>
                    全部采用建议
                  </button>
                )}
              </div>
              <button
                onClick={confirmReview}
                disabled={submitting || reviewIncomplete}
                type="button"
                style={btnPrimary(!submitting && !reviewIncomplete)}
              >
                {submitting ? '导入中…' : '确认修正并导入'}
              </button>
            </div>
          </>
        ) : (
          <>
            <Section label="工作流 JSON（ComfyUI · Export (API)）">
              <label
                ref={dropRef}
                style={dropZoneStyle}
                onDragOver={onDragOver}
                onDragLeave={onDragLeave}
                onDrop={onDrop}
              >
                <Upload size={16} style={{ color: 'var(--muted)', flexShrink: 0 }} />
                <span style={{ fontSize: 12, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {fileName || '点击选择 API 格式导出的 workflow.json'}
                </span>
                <input
                  data-testid="comfy-file-input"
                  type="file"
                  accept=".json,application/json"
                  onChange={onFileInputChange}
                  style={{ display: 'none' }}
                />
              </label>
              {fileError && <ErrorText>{fileError}</ErrorText>}
              {workflow && !fileError && (
                <div style={{ fontSize: 11, color: 'var(--accent-2, #22c55e)', marginTop: 6 }}>
                  已解析 {Object.keys(workflow).length} 个节点
                </div>
              )}
              {modelCheckWarning && <ErrorText>{modelCheckWarning}</ErrorText>}
            </Section>

            <Section label="服务名称 (对外 endpoint key)">
              <input
                value={name}
                onChange={(e) => setName(e.target.value.trim())}
                placeholder="服务名，例如：nous-minimax-h3-r2v"
                style={inputStyle}
              />
              {name && !nameValid && (
                <ErrorText>
                  {NAME_RULE_HINT}
                </ErrorText>
              )}
            </Section>

            {submitError && <ErrorText>{submitError}</ErrorText>}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 12 }}>
              <button onClick={onClose} type="button" style={btnGhost}>
                取消
              </button>
              <button
                onClick={submit}
                disabled={submitting || checkingModels}
                type="button"
                style={btnPrimary(!submitting && !checkingModels)}
              >
                {checkingModels ? '校验模型引用…' : submitting ? '导入中…' : '导入'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function isApiFormatWorkflow(parsed: unknown): parsed is ComfyWorkflow {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false
  const values = Object.values(parsed as Record<string, unknown>)
  if (values.length === 0) return false
  return values.every(
    (v) => !!v && typeof v === 'object' && !Array.isArray(v) && 'class_type' in (v as object),
  )
}

// ---------- shared bits (mirrors CreateServiceDialog's visual language) ----------

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div
        style={{
          fontSize: 11,
          color: 'var(--muted)',
          textTransform: 'uppercase',
          letterSpacing: 0.5,
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      {children}
    </div>
  )
}

function ErrorText({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 11, color: 'var(--error, #ef4444)', marginTop: 6 }}>{children}</div>
  )
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0,0,0,0.55)',
  zIndex: 50,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
}

const modalStyle: React.CSSProperties = {
  background: 'var(--bg-elevated, var(--bg))',
  border: '1px solid var(--border)',
  borderRadius: 8,
  width: 480,
  maxWidth: '92vw',
  maxHeight: '88vh',
  overflow: 'auto',
  padding: 20,
  boxShadow: '0 20px 50px rgba(0,0,0,0.5)',
}

const dropZoneStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  width: '100%',
  background: 'var(--bg)',
  color: 'var(--text)',
  border: '1px dashed var(--border)',
  borderRadius: 4,
  padding: '10px 12px',
  fontSize: 12,
  cursor: 'pointer',
  transition: 'border-color 0.15s',
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  background: 'var(--bg)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '7px 9px',
  fontSize: 12,
}

const issueRowStyle: React.CSSProperties = {
  border: '1px solid var(--border)',
  borderRadius: 6,
  padding: 10,
  background: 'var(--bg)',
}

const btnGhost: React.CSSProperties = {
  padding: '7px 14px',
  fontSize: 12,
  background: 'transparent',
  color: 'var(--muted)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  cursor: 'pointer',
}

const btnPrimary = (enabled: boolean): React.CSSProperties => ({
  padding: '7px 14px',
  fontSize: 12,
  background: 'var(--accent)',
  color: '#fff',
  border: 'none',
  borderRadius: 4,
  cursor: enabled ? 'pointer' : 'not-allowed',
  opacity: enabled ? 1 : 0.5,
})
