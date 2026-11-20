import type { ComfyExposedParam } from '../../api/comfyTemplates'

export const POWER_LORA_LOADER = 'Power Lora Loader (rgthree)'

export function powerLoraRows(inputs: Record<string, unknown>) {
  return Object.entries(inputs)
    .filter(([name, value]) => /^lora_\d+$/.test(name) && value && typeof value === 'object')
    .sort(([a], [b]) => a.length - b.length || a.localeCompare(b))
    .map(([, value]) => {
      const { lora, on, strength } = value as Record<string, unknown>
      return { lora, on, strength }
    })
}

export function mappingDependents(fields: ComfyExposedParam[], key: string) {
  return fields.filter(field => field.key !== key && (field.options_depends_on === key ||
    Object.hasOwn(field.visible_when ?? {}, key) || Object.hasOwn(field.required_when ?? {}, key)))
}
