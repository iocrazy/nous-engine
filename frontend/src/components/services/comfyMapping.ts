import type { ComfyExposedParam } from '../../api/comfyTemplates'

export function mappingDependents(fields: ComfyExposedParam[], key: string) {
  return fields.filter(field => field.key !== key && (field.options_depends_on === key ||
    Object.hasOwn(field.visible_when ?? {}, key) || Object.hasOwn(field.required_when ?? {}, key)))
}
