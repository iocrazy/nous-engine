import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './client'

// ---------- types ----------

export interface GrantSummary {
  // Snowflake IDs are serialized as strings server-side to dodge JS Number
  // precision loss (silent ±256 rounding on 18-digit ints made PATCH/DELETE
  // /grants/{id} 404 with the wrong row).
  id: string
  service_id: string
  service_name: string
  service_category: string | null
  status: 'active' | 'paused' | 'retired'
  activated_at: string
}

export interface ApiKeyRow {
  id: string
  label: string
  note: string | null
  key_prefix: string
  secret_plaintext: string | null
  is_active: boolean
  usage_calls: number
  usage_chars: number
  last_used_at: string | null
  created_at: string | null
  expires_at: string | null
  grant_count: number
  active_grant_count: number
  grants: GrantSummary[]
}

export interface ApiKeyCreated extends ApiKeyRow {
  secret: string
}

export interface CreateKeyBody {
  label: string
  note?: string | null
  expires_at?: string | null
  /** snowflake IDs — 用 string 避开 JS Number 精度问题；
   *  Pydantic 在 backend 会 str→int 无损还原。 */
  service_ids?: (number | string)[]
}

export interface PatchKeyBody {
  label?: string
  note?: string | null
  expires_at?: string | null
  is_active?: boolean
}

export interface ServiceGrantRow {
  grant_id: string
  api_key_id: string
  api_key_label: string
  api_key_prefix: string
  grant_status: 'active' | 'paused' | 'retired'
  activated_at: string
  pack_total: number
  pack_used: number
}

// ---------- queries ----------

export function useApiKeys() {
  return useQuery<ApiKeyRow[]>({
    queryKey: ['api-keys'],
    queryFn: () => apiFetch<ApiKeyRow[]>('/api/v1/keys'),
  })
}

export function useApiKey(keyId: number | string | null) {
  return useQuery<ApiKeyRow>({
    queryKey: ['api-key', String(keyId)],
    queryFn: () => apiFetch<ApiKeyRow>(`/api/v1/keys/${keyId}`),
    enabled: keyId != null,
  })
}

export function useServiceGrants(serviceId: number | string | null) {
  return useQuery<ServiceGrantRow[]>({
    queryKey: ['service-grants', String(serviceId)],
    queryFn: () => apiFetch<ServiceGrantRow[]>(`/api/v1/services/${serviceId}/grants`),
    enabled: serviceId != null,
  })
}

// ---------- mutations ----------

export function useCreateApiKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CreateKeyBody) =>
      apiFetch<ApiKeyCreated>('/api/v1/keys', {
        method: 'POST',
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })
}

export function useResetApiKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (keyId: number | string) =>
      apiFetch<ApiKeyCreated>(`/api/v1/keys/${keyId}/reset`, { method: 'POST' }),
    onSuccess: (_, keyId) => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
      qc.invalidateQueries({ queryKey: ['api-key', String(keyId)] })
    },
  })
}

export function usePatchApiKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ keyId, body }: { keyId: number | string; body: PatchKeyBody }) =>
      apiFetch<ApiKeyRow>(`/api/v1/keys/${keyId}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      }),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
      qc.invalidateQueries({ queryKey: ['api-key', String(vars.keyId)] })
    },
  })
}

export function useDeleteApiKey() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (keyId: number | string) =>
      apiFetch(`/api/v1/keys/${keyId}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })
}

// ---------- grant ops（复用 api_gateway 路由） ----------

export function useAddGrant() {
  const qc = useQueryClient()
  return useMutation({
    // serviceId 用 string|number — snowflake 大整数走 string 避免 JS 精度。
    // body 用 String() 强转后 JSON.stringify 不会丢精度；Pydantic
    // 接收时 str→int 还原。
    mutationFn: ({ keyId, serviceId }: { keyId: number | string; serviceId: number | string }) =>
      apiFetch(`/api/v1/keys/${keyId}/grants`, {
        method: 'POST',
        body: JSON.stringify({ instance_id: String(serviceId) }),
      }),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
      qc.invalidateQueries({ queryKey: ['api-key', String(vars.keyId)] })
      qc.invalidateQueries({ queryKey: ['service-grants', String(vars.serviceId)] })
    },
  })
}

export function useRemoveGrant() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (grantId: number | string) =>
      apiFetch(`/api/v1/grants/${grantId}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
      // The detail page subscribes to ['api-key', id]; without invalidating
      // it the row stays "active" until the user reloads.
      qc.invalidateQueries({ queryKey: ['api-key'] })
      qc.invalidateQueries({ queryKey: ['service-grants'] })
    },
  })
}

export function useToggleGrant() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ grantId, status }: { grantId: number | string; status: 'active' | 'paused' | 'retired' }) =>
      apiFetch(`/api/v1/grants/${grantId}`, {
        method: 'PATCH',
        body: JSON.stringify({ status }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['api-keys'] })
      qc.invalidateQueries({ queryKey: ['api-key'] })
      qc.invalidateQueries({ queryKey: ['service-grants'] })
    },
  })
}

// ---------- helpers ----------

export interface EndpointInfo {
  label: string
  url: string
  hint: string
}

/** 给某个 key + service 组合渲染调用 endpoint URL —— **按服务 category 给对端点**:
 *  llm→chat/completions(+ollama/anthropic) / embedding→embeddings / image→images
 *  generations / tts→audio/speech / rerank→rerank。null/未知 → 退回 chat(向后兼容)。 */
export function endpointsFor(
  serviceName: string,
  baseUrl: string,
  category?: string | null,
): Record<string, EndpointInfo> {
  const hint = `model: ${serviceName}`
  if (category === 'embedding') {
    return { embeddings: { label: 'OpenAI 兼容 · Embeddings', url: `${baseUrl}/v1/embeddings`, hint } }
  }
  if (category === 'rerank') {
    return { rerank: { label: 'Rerank (Cohere/Jina 兼容)', url: `${baseUrl}/v1/rerank`, hint } }
  }
  if (category === 'image') {
    return { images: { label: 'OpenAI 兼容 · Images', url: `${baseUrl}/v1/images/generations`, hint } }
  }
  if (category === 'tts') {
    return { audio: { label: 'OpenAI 兼容 · Speech', url: `${baseUrl}/v1/audio/speech`, hint } }
  }
  if (category === 'asr') {
    return { transcriptions: { label: 'OpenAI 兼容 · Transcriptions', url: `${baseUrl}/v1/audio/transcriptions`, hint } }
  }
  return {
    openai: {
      label: 'OpenAI 兼容',
      url: `${baseUrl}/v1/chat/completions`,
      hint,
    },
    ollama: {
      label: 'Ollama 兼容',
      url: `${baseUrl}/api/chat`,
      hint,
    },
    anthropic: {
      label: 'Anthropic 兼容',
      url: `${baseUrl}/v1/messages`,
      hint,
    },
  }
}
