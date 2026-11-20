import { useQuery } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { getComfyLoras } from '../../api/comfyTemplates'

type LoRARow = { lora: string; on: boolean; strength: number }

export default function LoRAListField({ value, onChange }: { value: unknown; onChange: (value: LoRARow[]) => void }) {
  const rows = Array.isArray(value) ? value as LoRARow[] : []
  const { data: files = [], isError, isPending } = useQuery({
    queryKey: ['comfy', 'loras'], queryFn: getComfyLoras, staleTime: 30_000,
  })
  const update = (index: number, patch: Partial<LoRARow>) =>
    onChange(rows.map((row, at) => at === index ? { ...row, ...patch } : row))

  return <div className="qwen-lora-list">
    {rows.map((row, index) => <div key={index} className="qwen-lora-row">
      <select aria-label={`LoRA ${index + 1} 文件`} value={row.lora}
        onChange={event => update(index, { lora: event.currentTarget.value })}>
        {!files.includes(row.lora) && <option value={row.lora}>{row.lora}</option>}
        {files.map(file => <option key={file} value={file}>{file}</option>)}
      </select>
      <label><input type="checkbox" aria-label={`启用 LoRA ${index + 1}`} checked={row.on}
        onChange={event => update(index, { on: event.currentTarget.checked })} /> 启用</label>
      <input type="number" aria-label={`LoRA ${index + 1} 强度`} value={row.strength}
        min={-10} max={10} step="any" required
        onChange={event => update(index, { strength: Number(event.currentTarget.value) })} />
      <button type="button" aria-label={`移除 LoRA ${index + 1}`}
        onClick={() => onChange(rows.filter((_, at) => at !== index))}><Trash2 size={14} /></button>
    </div>)}
    <button type="button" disabled={files.length === 0}
      onClick={() => onChange([...rows, { lora: files[0], on: false, strength: 1 }])}><Plus size={14} />添加 LoRA</button>
    {isError && <small role="status">无法读取 ComfyUI LoRA 文件清单</small>}
    {!isError && !isPending && files.length === 0 && <small>ComfyUI 未发现 LoRA 文件</small>}
  </div>
}
