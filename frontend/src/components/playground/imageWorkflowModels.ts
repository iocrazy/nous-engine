export const MOSS_IMAGE_ID = 'moss-image'
export const QWEN_TURBO_ID = 'qwen-image-2.1-turbo'

const QWEN_TURBO = 'Qwen Image 2.1 Turbo'
const QWEN_BASE = 'Qwen Image 2.1'
export const IMAGE_MODEL_NAMES: Record<string, string> = {
  [MOSS_IMAGE_ID]: 'MOSS Image', [QWEN_TURBO_ID]: QWEN_TURBO,
}
export const TOOL_MODELS: Record<string, string> = {
  edit: QWEN_TURBO, outpaint: QWEN_TURBO, remove: QWEN_TURBO,
  oldx: QWEN_BASE, character_card: QWEN_BASE, upscale: 'SeedVR2',
}
export const GENERATION_MODES = ['text', 'reference']
