import { describe, expect, it } from 'vitest'

import { endpointFor, NAME_RE, suggestServiceName } from './services'

describe('endpointFor — 服务卡端点提示按 category', () => {
  const ep = (category: string | null) => endpointFor({ name: 's', category: category as never })

  it('每个 model 类目落对的 OpenAI 兼容端点(2026-06-21:此前非 llm 全误落 /v1/apps)', () => {
    expect(ep('llm')).toContain('/v1/chat/completions')
    expect(ep('embedding')).toContain('/v1/embeddings')
    expect(ep('rerank')).toContain('/v1/rerank')
    expect(ep('tts')).toContain('/v1/audio/speech')
    expect(ep('asr')).toContain('/v1/audio/transcriptions')
    expect(ep('image')).toContain('/v1/images/generations')
  })

  it('app/workflow(无 model 类目)走 /v1/apps/.../run', () => {
    expect(ep('app')).toContain('/v1/apps/s/run')
    expect(ep(null)).toContain('/v1/apps/s/run')
  })

  it('asr 不串到 tts 的 speech 端点', () => {
    expect(ep('asr')).not.toContain('/v1/audio/speech')
  })
})

describe('NAME_RE — 服务名必须 nous- 开头(2026-09-27,与后端 workflow_snapshot.NAME_RE 同式)', () => {
  it('接受 nous- + 小写/数字/-,总长 ≤63', () => {
    for (const ok of ['nous-foo-bar', 'nous-x', 'nous-0', 'nous-' + 'a'.repeat(58)]) {
      expect(NAME_RE.test(ok), ok).toBe(true)
    }
    expect(('nous-' + 'a'.repeat(58)).length).toBe(63)
  })

  it('拒绝缺前缀、裸前缀、大写、超长', () => {
    for (const bad of ['foo-bar', 'nous-', 'nous', 'Nous-x', 'nous-Foo', 'nous--x', 'nous-' + 'a'.repeat(59)]) {
      expect(NAME_RE.test(bad), bad).toBe(false)
    }
  })
})

describe('suggestServiceName — 从模型 key 推导默认服务名', () => {
  it('slug 化后加 nous- 前缀与后缀', () => {
    expect(suggestServiceName('qwen3_8_27b_abliterated_awq', 'ab12')).toBe('nous-qwen3-8-27b-abliterated-awq-ab12')
  })

  it('已带 nous- 前缀不重复加', () => {
    expect(suggestServiceName('nous-embed', 'ab12')).toBe('nous-embed-ab12')
  })

  it('数字开头、大写、空串、超长都产出合法名', () => {
    for (const raw of ['3D Model', 'Qwen3.5 Chat', '', '___', 'x'.repeat(200), 'NOUS-Thing', 'nous-']) {
      const name = suggestServiceName(raw, 'zz99')
      expect(NAME_RE.test(name), `${raw} → ${name}`).toBe(true)
    }
  })
})
