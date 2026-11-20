import { useState } from 'react'
import { Download, Upload } from 'lucide-react'
import {
  previewComfyTemplateUpdate,
  reuploadComfyTemplate,
  type ComfyCanvasLayout,
  type ComfyExposedParam,
  type ComfyTemplateChanges,
  type ComfyTemplateDetail,
  type ComfyTemplateUpdate,
} from '../../api/comfyTemplates'
import { isComfyNodeRef, type ComfyWorkflow } from './comfyGraphLayout'
import { mappingDependents } from './comfyMapping'

function numericTuple(value: unknown, length: number): value is number[] {
  return Array.isArray(value) && value.length >= length &&
    value.slice(0, length).every(component => typeof component === 'number' && Number.isFinite(component))
}

function asCanvas(data: unknown): ComfyCanvasLayout | null {
  if (!data || typeof data !== 'object' || !('nodes' in data) || !Array.isArray(data.nodes)) return null
  const nodes = data.nodes.flatMap(node => {
    if (!node || (typeof node.id !== 'number' && typeof node.id !== 'string') ||
        typeof node.type !== 'string' || !numericTuple(node.pos, 2)) return []
    return [{ id: String(node.id), type: node.type, pos: [node.pos[0], node.pos[1]] as [number, number] }]
  })
  if (!nodes.length) throw new Error('原画布 JSON 中没有可读取的节点位置')
  const groups = 'groups' in data && Array.isArray(data.groups) ? data.groups.flatMap(group => {
    if (!group || typeof group.title !== 'string' || !numericTuple(group.bounding, 4)) return []
    const [x, y, width, height] = group.bounding
    if (width <= 0 || height <= 0) return []
    return [{ title: group.title, bounding: [x, y, width, height] as [number, number, number, number] }]
  }) : []
  return { nodes, groups, source: { ...data } }
}

function downloadWorkflow(detail: ComfyTemplateDetail) {
  const source = detail.canvas_layout?.source
  const content = JSON.stringify(source ?? detail.workflow_json, null, 2)
  const url = URL.createObjectURL(new Blob([content], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${detail.service_name}${source ? '.json' : '.api.json'}`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

function apiWorkflow(value: unknown): value is ComfyWorkflow {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const nodes = Object.values(value)
  return nodes.length > 0 && nodes.every(node => node && typeof node === 'object'
    && typeof node.class_type === 'string'
    && node.inputs && typeof node.inputs === 'object' && !Array.isArray(node.inputs))
}

function readFile(file: File): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('读取文件失败'))
    reader.onload = () => {
      try { resolve(JSON.parse(String(reader.result))) }
      catch { reject(new Error('不是合法 JSON')) }
    }
    reader.readAsText(file)
  })
}

function bindingChanged(field: ComfyExposedParam, before: ComfyWorkflow, after: ComfyWorkflow, ids: Set<string>) {
  const node = after[field.comfy_node_id]
  const old = before[field.comfy_node_id]
  if (!node || node.class_type !== old?.class_type || !Object.hasOwn(node.inputs, field.comfy_input)) return true
  const value = node.inputs[field.comfy_input]
  const oldValue = old.inputs[field.comfy_input]
  return isComfyNodeRef(value, ids) || typeof value !== typeof oldValue || Array.isArray(value) !== Array.isArray(oldValue)
}

function bindingOptions(field: ComfyExposedParam, workflow: ComfyWorkflow, fields: ComfyExposedParam[]) {
  const ids = new Set(Object.keys(workflow))
  const kind = field.type?.toLowerCase()
  return Object.entries(workflow).flatMap(([id, node]) => Object.entries(node.inputs ?? {}).flatMap(([input, value]) => {
    if (isComfyNodeRef(value, ids) || fields.some(other => other.key !== field.key
      && id === other.comfy_node_id && input === other.comfy_input)) return []
    const matches = kind === 'integer' ? typeof value === 'number' && Number.isInteger(value)
      : kind === 'number' ? typeof value === 'number'
      : kind === 'boolean' || kind === 'bool' ? typeof value === 'boolean'
      : typeof value === 'string'
    return matches ? [{ id, input, title: node._meta?.title ?? node.class_type }] : []
  }))
}

interface PendingUpdate {
  body: ComfyTemplateUpdate
  workflow: ComfyWorkflow
  changes: ComfyTemplateChanges
  mapping: ComfyExposedParam[]
  unresolved: string[]
}

export default function ComfyWorkflowUpdate({ detail, fields, busy, onBusy, onReview, onApplied }: {
  detail: ComfyTemplateDetail
  fields: ComfyExposedParam[]
  busy: boolean
  onBusy: (value: boolean) => void
  onReview: (value: boolean) => void
  onApplied: (detail: ComfyTemplateDetail, addedNodes: boolean) => void
}) {
  const [pending, setPending] = useState<PendingUpdate | null>(null)
  const [error, setError] = useState<string | null>(null)
  const before = detail.workflow_json as ComfyWorkflow

  const preview = async (files: File[]) => {
    setError(null)
    setPending(null)
    onBusy(true)
    try {
      if (files.length > 2) throw new Error('请选择一份 API JSON 和一份原画布 JSON')
      const body: ComfyTemplateUpdate = { revision: detail.revision }
      for (const data of await Promise.all(files.map(readFile))) {
        const canvas = asCanvas(data)
        if (canvas) {
          if (body.canvas_layout) throw new Error('一次只能选择一份原画布 JSON')
          body.canvas_layout = canvas
        } else if (apiWorkflow(data) && !body.workflow) body.workflow = data
        else throw new Error('请选择 ComfyUI 原画布 JSON 或 Export (API) 导出的 JSON')
      }
      const changes = await previewComfyTemplateUpdate(detail.id, body)
      const workflow = (body.workflow ?? before) as ComfyWorkflow
      const ids = new Set(Object.keys(workflow))
      const unresolved = fields.filter(field => (detail.stale_keys ?? []).includes(field.key) ||
        (body.workflow && (changes.stale_keys.includes(field.key) || bindingChanged(field, before, workflow, ids))))
        .map(field => field.key)
      setPending({ body, workflow, changes, mapping: fields, unresolved })
      onReview(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      onBusy(false)
    }
  }

  const resolve = (key: string, binding?: { id: string; input: string }) => {
    if (!pending) return
    const mapping = binding ? pending.mapping.map(field => field.key === key
      ? { ...field, comfy_node_id: binding.id, comfy_input: binding.input } : field)
      : pending.mapping.filter(field => field.key !== key)
    setPending({ ...pending, mapping, unresolved: pending.unresolved.filter(item => item !== key) })
    setError(null)
  }

  const apply = async () => {
    if (!pending || pending.unresolved.length) return
    setError(null)
    onBusy(true)
    try {
      const { body, workflow, mapping } = pending
      const result = await reuploadComfyTemplate(detail.id, { ...body, mapping: { exposed_params: mapping } })
      const canvas = body.canvas_layout ?? detail.canvas_layout
      const canvasLayout = result.has_canvas_source === false && canvas
        ? { ...canvas, source: undefined } : canvas
      onApplied({
        ...detail, workflow_json: workflow, canvas_layout: canvasLayout,
        revision: result.revision, stale_keys: result.stale_keys, exposed_params: mapping,
      }, pending.changes.added.length > 0)
      setPending(null)
      onReview(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      onBusy(false)
    }
  }

  return (
    <div className="comfy-workflow-upload">
      <button type="button" onClick={() => downloadWorkflow(detail)} disabled={busy}>
        <Download size={14} />
        下载工作流
      </button>
      <label>
        <Upload size={14} />
        更新工作流
        <input data-testid="comfy-reupload-input" aria-label="选择 ComfyUI 工作流文件" type="file"
          accept=".json,application/json" multiple disabled={busy || !!pending}
          onChange={event => {
            const files = Array.from(event.currentTarget.files ?? [])
            event.currentTarget.value = ''
            if (files.length) void preview(files)
          }} />
      </label>
      <span>
        在 ComfyUI 编辑模式打开工作流，修改后导出 API JSON 上传，可同时上传原画布 JSON。
        {!detail.canvas_layout?.source && ' 当前下载的是 API JSON。若要保留原画布布局和备注，请在 ComfyUI 打开原文件。'}
      </span>
      {error && <div role="alert">{error}</div>}
      {pending && (
        <section className="comfy-update-review" aria-label="工作流更新预览">
          <strong>工作流更新预览</strong>
          {([
            ['新增节点', pending.changes.added], ['删除节点', pending.changes.removed],
            ['节点类型变化', pending.changes.changed_classes], ['节点输入变化', pending.changes.changed_inputs],
          ] as const).map(([name, ids]) => (
            <details key={name} open={ids.length > 0}>
              <summary>{name} · {ids.length}</summary>
              {ids.map(id => {
                const node = pending.workflow[id] ?? before[id]
                return <div key={id}>#{id} · {node?._meta?.title ?? node?.class_type}</div>
              })}
            </details>
          ))}
          {!pending.body.workflow && <span>只更新画布。要应用节点增删，请同时上传 API JSON。</span>}
          {pending.changes.added.length > 0 && <span>应用后可在节点画布中为新增节点配置运行参数。</span>}
          {pending.body.canvas_layout && <span>{pending.body.canvas_layout.groups.length} 个原画布分组</span>}
          {pending.unresolved.length > 0 && (
            <strong data-testid="stale-keys-warning">失效字段 · {pending.unresolved.length}，请重新绑定或移除</strong>
          )}
          {pending.unresolved.map(key => {
            const field = pending.mapping.find(item => item.key === key)!
            const options = bindingOptions(field, pending.workflow, pending.mapping)
            const dependents = mappingDependents(pending.mapping, key)
            return (
              <div key={key} className="comfy-rebind-row">
                <span>
                  {field.label || key} <code>{key}</code>
                  <small>#{field.comfy_node_id} · {field.comfy_input} · {pending.workflow[field.comfy_node_id] ? '绑定已变化' : '节点已删除'}</small>
                </span>
                <select aria-label={`重新绑定 ${key}`} value="" disabled={busy || !options.length}
                  onChange={event => {
                    const binding = event.currentTarget.value === '' ? undefined : options[Number(event.currentTarget.value)]
                    if (binding) resolve(key, binding)
                  }}>
                  <option value="">{options.length ? '选择新的节点输入' : '没有兼容的节点输入'}</option>
                  {options.map(({ id, input, title }, index) =>
                    <option key={index} value={index}>#{id} {title} · {input}</option>)}
                </select>
                <button type="button" disabled={busy || dependents.length > 0}
                  title={dependents.length ? `被 ${dependents.map(item => item.key).join('、')} 引用` : undefined}
                  onClick={() => resolve(key)}>移除参数</button>
              </div>
            )
          })}
          <div className="comfy-editor-actions">
            <button type="button" disabled={busy} onClick={() => { setPending(null); setError(null); onReview(false) }}>取消更新</button>
            <button type="button" aria-busy={busy} disabled={busy || pending.unresolved.length > 0}
              onClick={() => void apply()}>应用更新</button>
          </div>
        </section>
      )}
    </div>
  )
}
