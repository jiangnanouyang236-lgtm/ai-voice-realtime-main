<template>
  <section class="page-grid single-column">
    <div class="card compact-page-header">
      <div>
        <p class="eyebrow">Runtime</p>
        <h3>当前运行状态</h3>
      </div>
      <button class="ghost-button" :disabled="loading" @click="loadData">
        {{ loading ? '刷新中...' : '刷新状态' }}
      </button>
    </div>

    <div v-if="error" class="card error-card">{{ error }}</div>

    <div v-if="isInitialLoading" class="page-grid single-column">
      <div class="stats-grid">
        <article v-for="index in 3" :key="index" class="card stat-card loading-card">
          <div class="skeleton-line skeleton-line-short"></div>
          <div class="skeleton-line skeleton-line-value"></div>
        </article>
      </div>
      <div class="page-grid two-column">
        <article class="card loading-card">
          <div class="loading-stack">
            <div class="skeleton-line skeleton-line-short"></div>
            <div class="skeleton-line skeleton-line-medium"></div>
            <div v-for="index in 4" :key="`session-${index}`" class="skeleton-row"></div>
          </div>
        </article>
        <article class="card loading-card">
          <div class="loading-stack">
            <div class="skeleton-line skeleton-line-short"></div>
            <div class="skeleton-line skeleton-line-medium"></div>
            <div v-for="index in 4" :key="`robot-${index}`" class="skeleton-row"></div>
          </div>
        </article>
      </div>
    </div>

    <template v-else>
      <div class="stats-grid">
        <article class="card stat-card">
          <p class="stat-label">Active Connections</p>
          <p class="stat-value">{{ payload?.stats.active_connections ?? 0 }}</p>
        </article>
        <article class="card stat-card">
          <p class="stat-label">Total Sessions</p>
          <p class="stat-value">{{ payload?.stats.total_sessions ?? 0 }}</p>
        </article>
        <article class="card stat-card">
          <p class="stat-label">Registered Sessions</p>
          <p class="stat-value">{{ payload?.stats.registered_sessions ?? 0 }}</p>
        </article>
      </div>

      <article class="card">
        <div class="section-header">
          <div>
            <p class="eyebrow">Gateway Settings</p>
            <h3>当前已加载参数</h3>
          </div>
        </div>

        <dl class="detail-list detail-grid-three">
          <div>
            <dt>Max Connections</dt>
            <dd>{{ payload?.settings?.max_connections ?? '-' }}</dd>
          </div>
          <div>
            <dt>Max History</dt>
            <dd>{{ payload?.settings?.max_history_length ?? '-' }}</dd>
          </div>
        </dl>
      </article>

      <article class="card">
        <div class="section-header">
          <div>
            <p class="eyebrow">Upstreams</p>
            <h3>上游 gRPC 连接</h3>
          </div>
        </div>

        <div v-if="upstreams.length === 0" class="empty-state">当前没有上游连接状态。</div>
        <div v-else class="status-list">
          <div v-for="item in upstreams" :key="item.service" class="status-row upstream-status-row">
            <div class="status-copy">
              <strong>{{ item.service.toUpperCase() }}</strong>
              <p>{{ item.target }}</p>
              <small>{{ item.message ?? '等待状态' }}</small>
              <small v-if="item.last_ready_at">last_ready={{ formatDate(item.last_ready_at) }}</small>
              <small v-if="item.last_error">
                last_error={{ item.last_error }}
                <span v-if="item.last_error_at">({{ formatDate(item.last_error_at) }})</span>
              </small>
            </div>
            <StatusBadge
              :label="item.status"
              :tone="item.status === 'ready' ? 'success' : item.status === 'error' ? 'danger' : 'neutral'"
            />
          </div>
        </div>
      </article>

      <article class="card">
        <div class="section-header">
          <div>
            <p class="eyebrow">Loaded Agents</p>
            <h3>LLM 已加载 Agent</h3>
          </div>
        </div>

        <div v-if="payload?.agents_error" class="empty-state inline-error">
          Agent 状态读取失败：{{ payload.agents_error }}
        </div>
        <div v-else-if="agents.length === 0" class="empty-state">当前 LLM runtime 没有上报 Agent。</div>
        <div v-else class="status-list">
          <div v-for="item in agents" :key="item.id" class="status-row">
            <div class="status-copy">
              <strong>{{ item.name }}</strong>
              <p>{{ item.id }}{{ item.class_name ? ` · ${item.class_name}` : '' }}</p>
              <small v-if="item.allowed_tools.length > 0">tools={{ item.allowed_tools.join(', ') }}</small>
              <small v-if="item.tool_groups.length > 0">groups={{ item.tool_groups.join(', ') }}</small>
            </div>
            <StatusBadge :label="item.active ? 'loaded' : 'inactive'" :tone="item.active ? 'success' : 'neutral'" />
          </div>
        </div>
      </article>

      <div class="page-grid two-column">
        <article class="card">
          <div class="section-header">
            <div>
              <p class="eyebrow">Sessions</p>
              <h3>活跃会话</h3>
            </div>
          </div>

          <div v-if="sessions.length === 0" class="empty-state">当前没有活跃会话。</div>
          <div v-else class="status-list">
            <button
              v-for="item in sessions"
              :key="item.session_id"
              class="status-row session-row"
              :class="{ active: item.session_id === selectedSessionId }"
              type="button"
              @click="selectSession(item.session_id)"
            >
              <div class="status-copy">
                <strong>{{ item.robot_id || item.session_id }}</strong>
                <p>bot={{ item.bot_id ?? '-' }} · {{ item.bot_name ?? '未注册' }}</p>
                <small>
                  client={{ item.client_type ?? '-' }} · history={{ item.history_count }} ·
                  last_active={{ formatDate(item.last_active_at) }}
                </small>
              </div>
              <div class="list-badges">
                <StatusBadge :label="item.registered ? 'registered' : 'anonymous'" :tone="item.registered ? 'success' : 'neutral'" />
                <StatusBadge v-if="item.interrupted" label="interrupted" tone="warning" />
                <StatusBadge v-if="item.is_new_robot" label="new" tone="warning" />
              </div>
            </button>
          </div>
        </article>

        <article class="card">
          <div class="section-header">
            <div>
              <p class="eyebrow">Runtime Robots</p>
              <h3>已加载 Robot 绑定</h3>
            </div>
          </div>

          <div v-if="robots.length === 0" class="empty-state">当前 runtime 没有已加载的 Robot。</div>
          <div v-else class="status-list">
            <div v-for="item in robots" :key="item.robot_id" class="status-row">
              <div class="status-copy">
                <strong>{{ item.name }}</strong>
                <p>{{ item.robot_id }} -> {{ item.assigned_bot_id }} {{ item.bot_name ? `(${item.bot_name})` : '' }}</p>
                <small>
                  client={{ item.client_type ?? '-' }} · last_connected={{ formatDate(item.last_connected_at) }}
                </small>
                <small v-if="item.notes">{{ item.notes }}</small>
              </div>
              <div class="list-badges">
                <StatusBadge :label="item.enabled ? 'enabled' : 'disabled'" :tone="item.enabled ? 'success' : 'danger'" />
                <StatusBadge v-if="item.is_new" label="new" tone="warning" />
              </div>
            </div>
          </div>
        </article>
      </div>

      <article class="card">
        <div class="section-header">
          <div>
            <p class="eyebrow">Conversation Preview</p>
            <h3>当前会话最近对话</h3>
          </div>
          <button
            v-if="selectedSessionId"
            class="ghost-button compact-button"
            :disabled="sessionDetailLoading"
            type="button"
            @click="loadSessionDetail(selectedSessionId)"
          >
            {{ sessionDetailLoading ? '加载中...' : '刷新对话' }}
          </button>
        </div>

        <p class="runtime-note">
          这里展示的是 Gateway 当前进程内存里的最近消息，不是历史归档；连接断开或服务重启后会清理。
        </p>

        <div v-if="sessions.length === 0" class="empty-state">当前没有可查看的活跃会话。</div>
        <div v-else-if="!selectedSessionId" class="empty-state">选择一个活跃会话后查看最近对话。</div>
        <div v-else-if="sessionDetailLoading" class="conversation-list">
          <div v-for="index in 3" :key="`message-${index}`" class="skeleton-row skeleton-row-compact"></div>
        </div>
        <div v-else-if="sessionDetailError" class="empty-state inline-error">{{ sessionDetailError }}</div>
        <div v-else-if="selectedSessionDetail" class="conversation-panel">
          <dl class="detail-list detail-grid-three conversation-meta">
            <div>
              <dt>Session</dt>
              <dd>{{ selectedSessionDetail.session_id }}</dd>
            </div>
            <div>
              <dt>Robot</dt>
              <dd>{{ selectedSessionDetail.robot_id ?? '-' }}</dd>
            </div>
            <div>
              <dt>Bot</dt>
              <dd>{{ selectedSessionDetail.bot_name ?? selectedSessionDetail.bot_id ?? '-' }}</dd>
            </div>
          </dl>

          <div v-if="selectedSessionDetail.history.length === 0" class="empty-state">
            这个会话还没有完成的对话记录。通常一轮 ASR → LLM → TTS 完成后才会写入。
          </div>
          <div v-else class="conversation-list">
            <div
              v-for="(message, index) in selectedSessionDetail.history"
              :key="`${message.role}-${index}-${message.timestamp ?? ''}`"
              class="conversation-message"
              :class="`role-${message.role}`"
            >
              <div class="message-head">
                <span class="role-pill">{{ formatRole(message.role) }}</span>
                <small>{{ formatDate(message.timestamp) }}</small>
              </div>
              <p>{{ message.content }}</p>
            </div>
          </div>
        </div>
      </article>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import StatusBadge from '../components/StatusBadge.vue'
import { fetchGatewayRuntime, fetchGatewaySessionDetail } from '../lib/api'
import type { RuntimeGatewayPayload, RuntimeGatewaySessionDetailItem } from '../types'

const payload = ref<RuntimeGatewayPayload | null>(null)
const loading = ref(false)
const error = ref('')
const selectedSessionId = ref<string | null>(null)
const selectedSessionDetail = ref<RuntimeGatewaySessionDetailItem | null>(null)
const sessionDetailLoading = ref(false)
const sessionDetailError = ref('')

const sessions = computed(() => payload.value?.sessions ?? [])
const robots = computed(() => payload.value?.robots ?? [])
const upstreams = computed(() => payload.value?.upstreams ?? [])
const agents = computed(() => payload.value?.agents ?? [])
const isInitialLoading = computed(() => loading.value && !payload.value)

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    payload.value = await fetchGatewayRuntime()
    const nextSessionId = resolveSelectedSessionId()
    selectedSessionId.value = nextSessionId
    if (nextSessionId) {
      await loadSessionDetail(nextSessionId)
    } else {
      selectedSessionDetail.value = null
      sessionDetailError.value = ''
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载 Runtime 失败'
  } finally {
    loading.value = false
  }
}

function resolveSelectedSessionId() {
  if (selectedSessionId.value && sessions.value.some((item) => item.session_id === selectedSessionId.value)) {
    return selectedSessionId.value
  }
  return sessions.value[0]?.session_id ?? null
}

async function selectSession(sessionId: string) {
  selectedSessionId.value = sessionId
  await loadSessionDetail(sessionId)
}

async function loadSessionDetail(sessionId: string) {
  sessionDetailLoading.value = true
  sessionDetailError.value = ''
  try {
    const detail = await fetchGatewaySessionDetail(sessionId)
    if (selectedSessionId.value !== sessionId) {
      return
    }
    if (!detail.success || !detail.session) {
      selectedSessionDetail.value = null
      sessionDetailError.value = detail.message ?? '会话不存在或已清理'
      return
    }
    selectedSessionDetail.value = detail.session
  } catch (err) {
    if (selectedSessionId.value === sessionId) {
      selectedSessionDetail.value = null
      sessionDetailError.value = err instanceof Error ? err.message : '加载会话详情失败'
    }
  } finally {
    if (selectedSessionId.value === sessionId) {
      sessionDetailLoading.value = false
    }
  }
}

function formatDate(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function formatRole(role: string) {
  if (role === 'user') {
    return 'User'
  }
  if (role === 'assistant') {
    return 'Assistant'
  }
  if (role === 'tool') {
    return 'Tool'
  }
  return role || 'Message'
}

onMounted(loadData)
</script>
