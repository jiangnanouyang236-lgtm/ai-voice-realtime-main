export type ApiMessage = {
  success: boolean
  message: string
  detail?: unknown
}

export type AuthStatusPayload = {
  success: boolean
  authenticated: boolean
  username?: string | null
  message?: string | null
}

export type RuntimeMcpStatusItem = {
  server_key?: string
  type?: string | null
  connected?: boolean
  loaded?: boolean
  tool_count?: number
  last_connected_at?: string | null
  last_error?: string | null
  last_error_at?: string | null
  status?: string | null
  message?: string | null
}

export type RuntimeServiceStatus = {
  success?: boolean
  source?: string | null
  config_version?: number | null
  loaded_at?: string | null
  bot_count?: number | null
  robot_count?: number | null
  mcp_count?: number | null
  default_bot_id?: string | null
  last_reload_error?: string | null
  tts_settings?: Record<string, unknown> | null
  request_count?: number | null
  last_request_at?: string | null
  last_error?: string | null
  last_error_at?: string | null
  gateway_settings?: Record<string, unknown> | null
  message?: string | null
}

export type OptionItem = {
  value: string
  label: string
}

export type ConfigSnapshotItem = {
  config_version: number
  created_at: string
  created_by?: string | null
}

export type OverviewPayload = {
  success: boolean
  db: {
    latest_config_version: number
    bot_count: number
    robot_count: number
    mcp_count: number
    agent_count?: number
    tts_profile_count?: number
    default_bot_id: string | null
  }
  runtime: {
    success?: boolean
    source?: string | null
    config_version?: number | null
    loaded_at?: string | null
    bot_count?: number | null
    mcp_count?: number | null
    default_bot_id?: string | null
    last_reload_error?: string | null
    mcp_status?: RuntimeMcpStatusItem[]
    in_sync?: boolean | null
    llm?: RuntimeServiceStatus
    gateway?: RuntimeServiceStatus
    tts?: RuntimeServiceStatus
  }
  snapshots: ConfigSnapshotItem[]
}

export type OptionsPayload = {
  success: boolean
  models: string[]
  tts_custom_voices: string[]
  tts_profiles: OptionItem[]
  mcp_types: string[]
  enabled_mcp_servers: OptionItem[]
  enabled_agents: OptionItem[]
  bots: OptionItem[]
}

export type BotItem = {
  bot_id: string
  name: string
  system_prompt: string
  model: string
  temperature: number
  max_tokens: number
  max_response_chars: number
  tts_profile_id: string
  enabled: boolean
  is_default: boolean
  mcp_servers: string[]
  agents: string[]
  updated_at: string
}

export type TTSProviderType = 'qwen3_custom_voice' | 'qwen3_base'

export type TTSProfileItem = {
  tts_id: string
  tts_name: string
  provider_type: TTSProviderType
  speed: number
  enabled: boolean
  voice?: string | null
  instruct?: string | null
  path?: string | null
  content?: string | null
  updated_at: string
}

export type AgentItem = {
  agent_id: string
  name: string
  description: string | null
  module: string | null
  class_name: string | null
  enabled: boolean
  enabled_by_default: boolean
  trigger_examples_json: string[]
  allowed_tools_json: string[]
  tool_groups_json: string[]
  updated_at: string
}

export type RobotItem = {
  robot_id: string
  name: string
  assigned_bot_id: string
  client_type: string | null
  enabled: boolean
  is_new: boolean
  notes: string | null
  last_connected_at: string | null
  updated_at: string
  secret_configured: boolean
  robot_secret_updated_at?: string | null
}

export type RobotSecretPayload = {
  success: boolean
  robot_id: string
  robot_secret: string
  robot_secret_updated_at?: string | null
  message: string
}

export type MCPServerItem = {
  server_key: string
  display_name: string
  type: 'sse' | 'streamable_http'
  url: string | null
  command?: string | null
  args_json?: string[]
  headers_json: Record<string, string>
  enabled: boolean
  updated_at: string
}

export type RuntimeGatewaySessionItem = {
  session_id: string
  robot_id: string | null
  bot_id: string | null
  bot_name: string | null
  client_type: string | null
  is_new_robot: boolean
  interrupted: boolean
  history_count: number
  last_active_at: string | null
  registered: boolean
}

export type RuntimeGatewayMessageItem = {
  role: string
  content: string
  timestamp?: string | null
}

export type RuntimeGatewaySessionDetailItem = RuntimeGatewaySessionItem & {
  history: RuntimeGatewayMessageItem[]
}

export type RuntimeGatewaySessionDetailPayload = {
  success: boolean
  session?: RuntimeGatewaySessionDetailItem | null
  message?: string | null
}

export type RuntimeGatewayRobotItem = {
  robot_id: string
  name: string
  assigned_bot_id: string
  bot_name: string | null
  client_type: string | null
  enabled: boolean
  is_new: boolean
  last_connected_at: string | null
  notes: string | null
}

export type RuntimeGatewayUpstreamItem = {
  service: string
  target: string
  secure?: boolean
  status: string
  message?: string | null
  last_ready_at?: string | null
  last_error?: string | null
  last_error_at?: string | null
}

export type RuntimeAgentItem = {
  id: string
  name: string
  description?: string | null
  module?: string | null
  class_name?: string | null
  trigger_examples?: string[]
  allowed_tools: string[]
  tool_groups: string[]
  active: boolean
}

export type RuntimeGatewayPayload = {
  success: boolean
  stats: {
    active_connections: number
    total_sessions: number
    registered_sessions: number
  }
  sessions: RuntimeGatewaySessionItem[]
  robots: RuntimeGatewayRobotItem[]
  settings?: Record<string, unknown> | null
  upstreams?: RuntimeGatewayUpstreamItem[]
  agents?: RuntimeAgentItem[]
  agents_error?: string | null
}

export type GatewayTraceStats = {
  enabled: boolean
  round_count: number
  event_count: number
  max_rounds: number
  max_events: number
  text_max_chars: number
  error_max_chars: number
}

export type GatewayTraceBreakdownItem = {
  metric: string
  label: string
  duration_ms: number | null
  slow_threshold_ms: number | null
  slow: boolean
}

export type GatewayTraceDiagnosis = {
  severity: string | null
  bottleneck_metric: string | null
  bottleneck_label: string | null
  bottleneck_ms: number | null
  signals: string[]
  breakdown: GatewayTraceBreakdownItem[]
}

export type GatewayTraceRound = {
  trace_id: string
  session_id: string
  round_seq: number | null
  robot_id: string | null
  bot_id: string | null
  bot_name: string | null
  started_at: string | null
  last_event_at: string | null
  last_stage: string | null
  status: string | null
  duration_ms: number | null
  event_count: number
  metrics: Record<string, unknown>
  diagnosis: GatewayTraceDiagnosis
}

export type GatewayTraceEvent = {
  trace_id: string
  session_id: string
  round_seq: number | null
  robot_id: string | null
  bot_id: string | null
  bot_name: string | null
  stage: string
  status: string
  ts: string
  ts_epoch: number
  duration_ms: number | null
  summary: Record<string, unknown>
  error: string | null
}

export type GatewayTraceDetail = GatewayTraceRound & {
  events: GatewayTraceEvent[]
}

export type GatewayTracePagination = {
  limit: number
  offset: number
  total: number
  has_more: boolean
}

export type GatewayTracesPayload = {
  success: boolean
  enabled: boolean
  items: GatewayTraceRound[]
  stats: GatewayTraceStats
  pagination?: GatewayTracePagination
  message?: string | null
}

export type GatewayTraceDetailPayload = {
  success: boolean
  enabled: boolean
  trace?: GatewayTraceDetail | null
  stats?: GatewayTraceStats
  message?: string | null
}
