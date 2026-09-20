<template>
  <section class="page-grid single-column">
    <div v-if="isInitialLoading" class="page-grid single-column">
      <div class="stats-grid">
        <article v-for="index in 7" :key="index" class="card stat-card loading-card">
          <div class="skeleton-line skeleton-line-short"></div>
          <div class="skeleton-line skeleton-line-value"></div>
        </article>
      </div>

      <div class="page-grid two-column">
        <article class="card loading-card">
          <div class="loading-stack">
            <div class="skeleton-line skeleton-line-short"></div>
            <div class="skeleton-line skeleton-line-medium"></div>
            <div v-for="index in 4" :key="`detail-${index}`" class="skeleton-line"></div>
          </div>
        </article>
        <article class="card loading-card">
          <div class="loading-stack">
            <div class="skeleton-line skeleton-line-short"></div>
            <div class="skeleton-line skeleton-line-medium"></div>
            <div v-for="index in 3" :key="`mcp-${index}`" class="skeleton-row"></div>
          </div>
        </article>
      </div>

      <article class="card loading-card">
        <div class="loading-stack">
          <div class="skeleton-line skeleton-line-short"></div>
          <div class="skeleton-line skeleton-line-medium"></div>
          <div v-for="index in 4" :key="`snapshot-${index}`" class="skeleton-row skeleton-row-compact"></div>
        </div>
      </article>
    </div>

    <template v-else>
    <div class="card compact-page-header">
      <div>
        <p class="eyebrow">Overview</p>
        <h3>运行版本与配置发布</h3>
      </div>
      <button class="primary-button" :disabled="reloading" @click="handleReload">
        {{ reloading ? 'Applying...' : 'Apply / Reload' }}
      </button>
    </div>

    <div v-if="error" class="card error-card">{{ error }}</div>
    <div v-if="successMessage" class="card success-card">{{ successMessage }}</div>

    <div class="stats-grid">
      <article class="card stat-card">
        <p class="stat-label">Latest Config Version</p>
        <p class="stat-value">v{{ overview?.db.latest_config_version ?? '-' }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">Loaded Runtime Version</p>
        <p class="stat-value">{{ runtimeVersionLabel }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">Bot Templates</p>
        <p class="stat-value">{{ overview?.db.bot_count ?? '-' }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">Robots</p>
        <p class="stat-value">{{ overview?.db.robot_count ?? '-' }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">MCP Servers</p>
        <p class="stat-value">{{ overview?.db.mcp_count ?? '-' }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">Agents</p>
        <p class="stat-value">{{ overview?.db.agent_count ?? '-' }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">TTS Profiles</p>
        <p class="stat-value">{{ overview?.db.tts_profile_count ?? '-' }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">Default Bot</p>
        <p class="stat-value compact">{{ overview?.db.default_bot_id ?? '-' }}</p>
      </article>
    </div>

    <div class="page-grid two-column">
      <article class="card">
        <div class="section-header">
          <div>
            <p class="eyebrow">Diff</p>
            <h3>Save / Apply 状态</h3>
          </div>
          <StatusBadge :label="versionStatus.label" :tone="versionStatus.tone" />
        </div>

        <dl class="detail-list">
          <div>
            <dt>配置来源</dt>
            <dd>{{ overview?.runtime.source ?? '-' }}</dd>
          </div>
          <div>
            <dt>最近加载时间</dt>
            <dd>{{ formatDate(overview?.runtime.loaded_at) }}</dd>
          </div>
          <div>
            <dt>LLM Runtime Version</dt>
            <dd>v{{ overview?.runtime.llm?.config_version ?? '-' }}</dd>
          </div>
          <div>
            <dt>Gateway Runtime Version</dt>
            <dd>v{{ overview?.runtime.gateway?.config_version ?? '-' }}</dd>
          </div>
          <div>
            <dt>TTS Runtime Version</dt>
            <dd>v{{ overview?.runtime.tts?.config_version ?? '-' }}</dd>
          </div>
          <div>
            <dt>Runtime 默认 Bot</dt>
            <dd>{{ overview?.runtime.default_bot_id ?? '-' }}</dd>
          </div>
          <div>
            <dt>最近加载错误</dt>
            <dd>{{ overview?.runtime.last_reload_error ?? '无' }}</dd>
          </div>
        </dl>
      </article>

      <article class="card">
        <div class="section-header">
          <div>
            <p class="eyebrow">Runtime MCP</p>
            <h3>加载状态</h3>
          </div>
        </div>

        <div v-if="mcpStatus.length === 0" class="empty-state">当前没有可展示的 MCP 运行状态。</div>
        <div v-else class="status-list">
          <div
            v-for="(item, index) in mcpStatus"
            :key="String(item.server_key ?? index)"
            class="status-row mcp-status-row"
          >
            <div class="status-copy">
              <strong>{{ item.server_key ?? 'unknown' }}</strong>
              <p>{{ item.message ?? '等待状态' }}</p>
              <small>
                type={{ item.type ?? '-' }} · tools={{ item.tool_count ?? 0 }} ·
                last_connected={{ formatDate(item.last_connected_at) }}
              </small>
              <small v-if="item.last_error">
                last_error={{ item.last_error }}
                <span v-if="item.last_error_at">({{ formatDate(item.last_error_at) }})</span>
              </small>
            </div>
            <StatusBadge
              :label="String(item.status ?? (item.connected ? 'connected' : item.loaded ? 'loaded' : 'idle'))"
              :tone="item.connected || item.loaded ? 'success' : item.last_error ? 'danger' : 'neutral'"
            />
            <button
              class="ghost-button compact-button"
              :disabled="isReconnecting(String(item.server_key ?? ''))"
              @click="handleReconnectMcp(String(item.server_key ?? ''))"
            >
              {{ isReconnecting(String(item.server_key ?? '')) ? 'Reconnecting...' : 'Reconnect' }}
            </button>
          </div>
        </div>
      </article>
    </div>

    <article class="card">
      <div class="section-header">
        <div>
          <p class="eyebrow">History</p>
          <h3>最近保存快照</h3>
        </div>
      </div>

      <div v-if="snapshotItems.length === 0" class="empty-state">当前还没有可展示的配置快照。</div>
      <div v-else class="status-list">
        <div
          v-for="item in snapshotItems"
          :key="item.config_version"
          class="status-row"
        >
          <div class="status-copy">
            <strong>v{{ item.config_version }}</strong>
            <p>created_by={{ item.created_by ?? 'unknown' }}</p>
            <small>created_at={{ formatDate(item.created_at) }}</small>
          </div>
          <div class="list-badges">
            <StatusBadge
              v-if="item.config_version === overview?.db.latest_config_version"
              label="latest"
              tone="warning"
            />
            <StatusBadge
              v-if="item.config_version === overview?.runtime.config_version"
              label="loaded"
              tone="success"
            />
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
import { fetchOverview, reconnectMcpServer, reloadRuntime } from '../lib/api'
import type { OverviewPayload } from '../types'

const overview = ref<OverviewPayload | null>(null)
const loading = ref(false)
const reloading = ref(false)
const reconnectingMcp = ref<Set<string>>(new Set())
const error = ref('')
const successMessage = ref('')

const mcpStatus = computed(() => overview.value?.runtime.mcp_status ?? [])
const snapshotItems = computed(() => overview.value?.snapshots ?? [])
const isInitialLoading = computed(() => loading.value && !overview.value)

const versionStatus = computed(() => {
  const latest = overview.value?.db.latest_config_version
  const loaded = overview.value?.runtime.config_version
  if (overview.value?.runtime.in_sync === false) {
    return { label: 'service mismatch', tone: 'danger' as const }
  }
  if (latest == null || loaded == null) {
    return { label: 'unknown', tone: 'neutral' as const }
  }
  if (latest === loaded) {
    return { label: 'in sync', tone: 'success' as const }
  }
  return { label: `pending v${latest - loaded}`, tone: 'warning' as const }
})

const runtimeVersionLabel = computed(() => {
  if (overview.value?.runtime.in_sync === false) {
    const llmVersion = overview.value?.runtime.llm?.config_version ?? '-'
    const gatewayVersion = overview.value?.runtime.gateway?.config_version ?? '-'
    const ttsVersion = overview.value?.runtime.tts?.config_version ?? '-'
    return `L${llmVersion}/G${gatewayVersion}/T${ttsVersion}`
  }
  const version = overview.value?.runtime.config_version
  return version == null ? '-' : `v${version}`
})

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    overview.value = await fetchOverview()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function handleReload() {
  reloading.value = true
  error.value = ''
  successMessage.value = ''
  try {
    const result = await reloadRuntime()
    if (!result.success) {
      throw new Error(result.message)
    }
    await loadData()
    const detail = typeof result.detail === 'object' && result.detail ? (result.detail as Record<string, unknown>) : {}
    const version =
      detail.in_sync === false
        ? `L${String((detail.llm as Record<string, unknown> | undefined)?.config_version ?? '-')
        }/G${String((detail.gateway as Record<string, unknown> | undefined)?.config_version ?? '-')}`
        : detail.config_version != null
          ? `v${String(detail.config_version)}`
          : 'unknown version'
    const loadedAt = typeof detail.loaded_at === 'string' ? formatDate(detail.loaded_at) : '-'
    successMessage.value = `Apply 成功，当前运行版本 ${version}，加载时间 ${loadedAt}`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '应用配置失败'
  } finally {
    reloading.value = false
  }
}

function isReconnecting(serverKey: string) {
  return reconnectingMcp.value.has(serverKey)
}

async function handleReconnectMcp(serverKey: string) {
  if (!serverKey || reconnectingMcp.value.has(serverKey)) {
    return
  }
  error.value = ''
  successMessage.value = ''
  reconnectingMcp.value = new Set([...reconnectingMcp.value, serverKey])
  try {
    const result = await reconnectMcpServer(serverKey)
    if (!result.success) {
      throw new Error(result.message)
    }
    successMessage.value = `${serverKey} 重连成功`
    await loadData()
  } catch (err) {
    error.value = err instanceof Error ? err.message : `${serverKey} 重连失败`
    await loadData()
  } finally {
    const next = new Set(reconnectingMcp.value)
    next.delete(serverKey)
    reconnectingMcp.value = next
  }
}

function formatDate(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

onMounted(loadData)
</script>
