import type {
  ApiMessage,
  AgentItem,
  AuthStatusPayload,
  BotItem,
  GatewayTraceDetailPayload,
  GatewayTracesPayload,
  MCPServerItem,
  OptionsPayload,
  OverviewPayload,
  RuntimeGatewayPayload,
  RuntimeGatewaySessionDetailPayload,
  RobotItem,
  RobotSecretPayload,
  TTSProfileItem,
} from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
const API_TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS ?? 12000)

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS)

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers ?? {}),
      },
      credentials: 'include',
      ...init,
      signal: init?.signal ?? controller.signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error(`请求超时，请稍后重试：${path}`)
    }
    throw err
  } finally {
    window.clearTimeout(timeout)
  }

  const payload = (await response.json().catch(() => null)) as Record<string, unknown> | null
  if (!response.ok) {
    const detail =
      typeof payload?.detail === 'string'
        ? payload.detail
        : typeof payload?.message === 'string'
          ? payload.message
          : response.statusText
    throw new Error(detail || '请求失败')
  }
  return payload as T
}

export async function fetchOverview() {
  return request<OverviewPayload>('/api/config/overview')
}

export async function fetchAuthStatus() {
  return request<AuthStatusPayload>('/api/auth/me')
}

export async function loginAdmin(username: string, password: string) {
  return request<AuthStatusPayload>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

export async function logoutAdmin() {
  return request<ApiMessage>('/api/auth/logout', { method: 'POST' })
}

export async function reloadRuntime() {
  return request<ApiMessage>('/api/config/reload', { method: 'POST' })
}

export async function reconnectMcpServer(serverKey: string) {
  return request<ApiMessage>(`/api/runtime/mcp/${encodeURIComponent(serverKey)}/reconnect`, { method: 'POST' })
}

export async function fetchGatewayRuntime() {
  return request<RuntimeGatewayPayload>('/api/runtime/gateway')
}

export async function fetchGatewaySessionDetail(sessionId: string) {
  return request<RuntimeGatewaySessionDetailPayload>(
    `/api/runtime/gateway/sessions/${encodeURIComponent(sessionId)}`,
  )
}

export async function fetchGatewayTraces(limit = 100, offset = 0) {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  })
  return request<GatewayTracesPayload>(`/api/runtime/traces?${params.toString()}`)
}

export async function fetchGatewayTraceDetail(traceId: string) {
  return request<GatewayTraceDetailPayload>(`/api/runtime/traces/${encodeURIComponent(traceId)}`)
}

export async function fetchOptions() {
  return request<OptionsPayload>('/api/options')
}

export async function fetchTtsProfiles() {
  const payload = await request<{ success: boolean; items: TTSProfileItem[] }>('/api/tts-profiles')
  return payload.items
}

export async function saveTtsProfile(item: Omit<TTSProfileItem, 'updated_at'>, isNew: boolean) {
  return request<{ success: boolean; item: TTSProfileItem }>(
    isNew ? '/api/tts-profiles' : `/api/tts-profiles/${encodeURIComponent(item.tts_id)}`,
    {
      method: isNew ? 'POST' : 'PUT',
      body: JSON.stringify(item),
    },
  )
}

export async function deleteTtsProfile(ttsId: string) {
  return request<ApiMessage>(`/api/tts-profiles/${encodeURIComponent(ttsId)}`, { method: 'DELETE' })
}

export async function fetchAgents() {
  const payload = await request<{ success: boolean; items: AgentItem[] }>('/api/agents')
  return payload.items
}

export async function saveAgent(item: Omit<AgentItem, 'updated_at'>) {
  return request<{ success: boolean; item: AgentItem }>(`/api/agents/${encodeURIComponent(item.agent_id)}`, {
    method: 'PUT',
    body: JSON.stringify(item),
  })
}

export async function fetchBots() {
  const payload = await request<{ success: boolean; items: BotItem[] }>('/api/bots')
  return payload.items
}

export async function saveBot(item: Omit<BotItem, 'updated_at'>, isNew: boolean) {
  return request<{ success: boolean; item: BotItem }>(isNew ? '/api/bots' : `/api/bots/${item.bot_id}`, {
    method: isNew ? 'POST' : 'PUT',
    body: JSON.stringify(item),
  })
}

export async function deleteBot(botId: string) {
  return request<ApiMessage>(`/api/bots/${encodeURIComponent(botId)}`, { method: 'DELETE' })
}

export async function fetchRobots() {
  const payload = await request<{ success: boolean; items: RobotItem[] }>('/api/robots')
  return payload.items
}

export async function saveRobot(
  item: Omit<RobotItem, 'last_connected_at' | 'updated_at' | 'secret_configured' | 'robot_secret_updated_at'>,
  isNew: boolean,
) {
  return request<{ success: boolean; item: RobotItem }>(
    isNew ? '/api/robots' : `/api/robots/${item.robot_id}`,
    {
      method: isNew ? 'POST' : 'PUT',
      body: JSON.stringify(item),
    },
  )
}

export async function resetRobotSecret(robotId: string) {
  return request<RobotSecretPayload>(`/api/robots/${encodeURIComponent(robotId)}/secret/reset`, {
    method: 'POST',
  })
}

export async function deleteRobot(robotId: string) {
  return request<ApiMessage>(`/api/robots/${encodeURIComponent(robotId)}`, { method: 'DELETE' })
}

export async function fetchMcpServers() {
  const payload = await request<{ success: boolean; items: MCPServerItem[] }>('/api/mcp-servers')
  return payload.items
}

export async function saveMcpServer(item: Omit<MCPServerItem, 'updated_at'>, isNew: boolean) {
  return request<{ success: boolean; item: MCPServerItem }>(
    isNew ? '/api/mcp-servers' : `/api/mcp-servers/${item.server_key}`,
    {
      method: isNew ? 'POST' : 'PUT',
      body: JSON.stringify(item),
    },
  )
}

export async function deleteMcpServer(serverKey: string) {
  return request<ApiMessage>(`/api/mcp-servers/${encodeURIComponent(serverKey)}`, { method: 'DELETE' })
}
