import { describe, expect, it } from 'vitest'

import { endpointsFor } from './keys'

describe('endpointsFor — 按服务 category 给对端点', () => {
  const base = 'https://api.example.com'

  it('asr → /v1/audio/transcriptions(2026-06-20 语音识别接入)', () => {
    const eps = endpointsFor('my-asr', base, 'asr')
    expect(eps.transcriptions).toBeTruthy()
    expect(eps.transcriptions.url).toBe(`${base}/v1/audio/transcriptions`)
    // asr 不应落回 chat
    expect(eps.openai).toBeUndefined()
  })

  it('embedding → /v1/embeddings', () => {
    expect(endpointsFor('emb', base, 'embedding').embeddings.url).toBe(`${base}/v1/embeddings`)
  })

  it('rerank → /v1/rerank', () => {
    expect(endpointsFor('rr', base, 'rerank').rerank.url).toBe(`${base}/v1/rerank`)
  })

  it('tts → /v1/audio/speech(与 asr 区分,别串)', () => {
    expect(endpointsFor('tts', base, 'tts').audio.url).toBe(`${base}/v1/audio/speech`)
  })

  it('null/未知 category → 退回 chat/completions', () => {
    expect(endpointsFor('x', base, null).openai.url).toBe(`${base}/v1/chat/completions`)
  })
})
