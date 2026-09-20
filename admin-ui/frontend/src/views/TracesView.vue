<template>
  <section class="page-grid single-column">
    <div class="card compact-page-header">
      <div>
        <p class="eyebrow">Traces</p>
        <h3>语音链路黑匣子</h3>
      </div>
      <button class="ghost-button" :disabled="loading" @click="loadData">
        {{ loading ? '刷新中...' : '刷新链路' }}
      </button>
    </div>

    <div v-if="error" class="card error-card">{{ error }}</div>

    <div class="stats-grid">
      <article class="card stat-card">
        <p class="stat-label">Trace Rounds</p>
        <p class="stat-value">{{ payload?.stats.round_count ?? 0 }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">Trace Events</p>
        <p class="stat-value">{{ payload?.stats.event_count ?? 0 }}</p>
      </article>
      <article class="card stat-card">
        <p class="stat-label">Capacity</p>
        <p class="stat-value">{{ payload?.stats.max_events ?? '-' }}</p>
        <p class="stat-note">events max · rounds max {{ payload?.stats.max_rounds ?? '-' }}</p>
      </article>
    </div>

    <div v-if="isInitialLoading" class="page-grid two-column">
      <article class="card loading-card">
        <div class="loading-stack">
          <div class="skeleton-line skeleton-line-short"></div>
          <div v-for="index in 5" :key="`trace-${index}`" class="skeleton-row"></div>
        </div>
      </article>
      <article class="card loading-card">
        <div class="loading-stack">
          <div class="skeleton-line skeleton-line-medium"></div>
          <div v-for="index in 6" :key="`event-${index}`" class="skeleton-row skeleton-row-compact"></div>
        </div>
      </article>
    </div>

    <template v-else>
      <div v-if="payload && !payload.enabled" class="card error-card">
        Gateway trace 当前未开启。可以通过 TRACE_ENABLED=true 开启。
      </div>

      <div class="page-grid two-column">
        <article class="card">
          <div class="section-header">
            <div>
              <p class="eyebrow">Recent Rounds</p>
              <h3>最近链路</h3>
            </div>
          </div>

          <div v-if="payload" class="trace-pagination">
            <div class="pagination-copy">
              <strong>{{ currentPage }} / {{ totalPages }}</strong>
              <small>共 {{ totalRounds }} 轮</small>
            </div>
            <label class="pagination-size">
              <span>每页</span>
              <select v-model.number="pageSize" @change="changePageSize">
                <option v-for="option in pageSizeOptions" :key="option" :value="option">
                  {{ option }}
                </option>
              </select>
            </label>
            <div class="pagination-actions">
              <button
                class="ghost-button compact-button"
                type="button"
                :disabled="loading || !canGoPrevious"
                @click="goToPage(currentPage - 1)"
              >
                上一页
              </button>
              <button
                class="ghost-button compact-button"
                type="button"
                :disabled="loading || !canGoNext"
                @click="goToPage(currentPage + 1)"
              >
                下一页
              </button>
            </div>
          </div>

          <div v-if="traces.length === 0" class="empty-state">当前还没有 trace 事件。</div>
          <div v-else class="status-list">
            <button
              v-for="item in traces"
              :key="item.trace_id"
              class="status-row session-row trace-row"
              :class="{ active: item.trace_id === selectedTraceId }"
              type="button"
              @click="selectTrace(item.trace_id)"
            >
              <div class="status-copy">
                <strong>{{ item.robot_id || item.session_id }}</strong>
                <p>
                  {{ item.trace_id }} · bot={{ item.bot_name ?? item.bot_id ?? '-' }}
                </p>
                <small>
                  stage={{ item.last_stage ?? '-' }} · events={{ item.event_count }} ·
                  total={{ formatDuration(item.duration_ms) }}
                </small>
                <small>
                  bottleneck={{ formatDiagnosisBottleneck(item.diagnosis) }}
                </small>
                <small>{{ formatDate(item.started_at) }}</small>
              </div>
              <StatusBadge
                :label="item.diagnosis?.severity ?? item.status ?? 'unknown'"
                :tone="diagnosisTone(item.diagnosis?.severity ?? item.status)"
              />
            </button>
          </div>
        </article>

        <article class="card">
          <div class="section-header">
            <div>
              <p class="eyebrow">Timeline</p>
              <h3>单轮时间线</h3>
            </div>
            <button
              v-if="selectedTraceId"
              class="ghost-button compact-button"
              :disabled="detailLoading"
              type="button"
              @click="loadTraceDetail(selectedTraceId)"
            >
              {{ detailLoading ? '加载中...' : '刷新详情' }}
            </button>
          </div>

          <p class="runtime-note">
            这里仅展示 Gateway 内存里的轻量摘要，不包含音频、完整 prompt 或完整工具结果。
          </p>

          <div v-if="!selectedTraceId" class="empty-state">选择一轮链路后查看时间线。</div>
          <div v-else-if="detailLoading" class="conversation-list">
            <div v-for="index in 6" :key="`detail-${index}`" class="skeleton-row skeleton-row-compact"></div>
          </div>
          <div v-else-if="detailError" class="empty-state inline-error">{{ detailError }}</div>
          <div v-else-if="selectedTrace" class="trace-detail">
            <dl class="detail-list detail-grid-three conversation-meta">
              <div>
                <dt>Trace</dt>
                <dd>{{ selectedTrace.trace_id }}</dd>
              </div>
              <div>
                <dt>Robot</dt>
                <dd>{{ selectedTrace.robot_id ?? '-' }}</dd>
              </div>
              <div>
                <dt>Total</dt>
                <dd>{{ formatDuration(selectedTrace.duration_ms) }}</dd>
              </div>
            </dl>

            <div
              v-if="selectedTrace.diagnosis"
              class="trace-diagnosis"
              :class="`trace-diagnosis-${selectedTrace.diagnosis.severity ?? 'unknown'}`"
            >
              <div class="trace-diagnosis-head">
                <div>
                  <p class="eyebrow">Diagnosis</p>
                  <strong>{{ formatDiagnosisBottleneck(selectedTrace.diagnosis) }}</strong>
                </div>
                <StatusBadge
                  :label="selectedTrace.diagnosis.severity ?? 'unknown'"
                  :tone="diagnosisTone(selectedTrace.diagnosis.severity)"
                />
              </div>
              <div v-if="selectedTrace.diagnosis.signals.length > 0" class="trace-signal-list">
                <span
                  v-for="signal in selectedTrace.diagnosis.signals"
                  :key="signal"
                  class="soft-badge trace-signal"
                >
                  {{ signal }}
                </span>
              </div>
              <div v-if="selectedTrace.diagnosis.breakdown.length > 0" class="trace-breakdown">
                <div
                  v-for="item in selectedTrace.diagnosis.breakdown"
                  :key="item.metric"
                  class="trace-breakdown-item"
                  :class="{ 'trace-breakdown-slow': item.slow }"
                >
                  <span>{{ item.label }}</span>
                  <strong>{{ formatDuration(item.duration_ms) }}</strong>
                </div>
              </div>
            </div>

            <div class="trace-timeline">
              <div
                v-for="item in timelineEvents"
                :key="`${item.event.stage}-${item.event.ts_epoch}`"
                class="trace-event"
                :class="{
                  'trace-event-error': item.event.status === 'error',
                  'trace-event-timeout': item.event.status === 'timeout',
                  'trace-event-slow': isSlowEvent(item.event),
                }"
              >
                <div class="trace-event-head">
                  <strong>{{ item.event.stage }}</strong>
                  <div class="list-badges">
                    <span v-if="item.gapMs !== null" class="soft-badge trace-gap">
                      +{{ formatDuration(item.gapMs) }}
                    </span>
                    <StatusBadge :label="item.event.status" :tone="badgeTone(item.event.status)" />
                    <span v-if="isSlowEvent(item.event)" class="soft-badge">slow</span>
                  </div>
                </div>
                <small>
                  {{ formatDate(item.event.ts) }} ·
                  gap={{ item.gapMs === null ? '-' : formatDuration(item.gapMs) }} ·
                  duration={{ formatDuration(item.event.duration_ms) }}
                </small>
                <pre v-if="Object.keys(item.event.summary || {}).length">{{ formatSummary(item.event.summary) }}</pre>
                <p v-if="item.event.error" class="inline-error">{{ item.event.error }}</p>
              </div>
            </div>
          </div>
        </article>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import StatusBadge from '../components/StatusBadge.vue'
import { fetchGatewayTraceDetail, fetchGatewayTraces } from '../lib/api'
import type {
  GatewayTraceDetail,
  GatewayTraceDiagnosis,
  GatewayTraceEvent,
  GatewayTracesPayload,
} from '../types'

const payload = ref<GatewayTracesPayload | null>(null)
const selectedTrace = ref<GatewayTraceDetail | null>(null)
const selectedTraceId = ref<string | null>(null)
const loading = ref(false)
const detailLoading = ref(false)
const error = ref('')
const detailError = ref('')
const pageSizeOptions = [25, 50, 100, 200]
const pageSize = ref(50)
const currentPage = ref(1)

const traces = computed(() => payload.value?.items ?? [])
const isInitialLoading = computed(() => loading.value && !payload.value)
const pageOffset = computed(() => (currentPage.value - 1) * pageSize.value)
const pagination = computed(() => payload.value?.pagination)
const totalRounds = computed(() => pagination.value?.total ?? payload.value?.stats.round_count ?? traces.value.length)
const totalPages = computed(() => Math.max(1, Math.ceil(totalRounds.value / pageSize.value)))
const canGoPrevious = computed(() => currentPage.value > 1)
const canGoNext = computed(() => pagination.value?.has_more ?? currentPage.value < totalPages.value)
const timelineEvents = computed(() => {
  const events = selectedTrace.value?.events ?? []
  return events.map((event, index) => ({
    event,
    gapMs: index === 0 ? null : getEventGapMs(events[index - 1], event),
  }))
})

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    payload.value = await fetchGatewayTraces(pageSize.value, pageOffset.value)
    await clampPageAfterLoad()
    const nextTraceId = resolveSelectedTraceId()
    selectedTraceId.value = nextTraceId
    if (nextTraceId) {
      await loadTraceDetail(nextTraceId)
    } else {
      selectedTrace.value = null
      detailError.value = ''
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载 Traces 失败'
  } finally {
    loading.value = false
  }
}

async function clampPageAfterLoad() {
  const page = payload.value?.pagination
  if (!page || page.total === 0 || page.offset < page.total) {
    return
  }
  currentPage.value = Math.max(1, Math.ceil(page.total / pageSize.value))
  payload.value = await fetchGatewayTraces(pageSize.value, pageOffset.value)
}

function resolveSelectedTraceId() {
  if (selectedTraceId.value && traces.value.some((item) => item.trace_id === selectedTraceId.value)) {
    return selectedTraceId.value
  }
  return traces.value[0]?.trace_id ?? null
}

async function goToPage(page: number) {
  const nextPage = Math.min(Math.max(1, page), totalPages.value)
  if (nextPage === currentPage.value && payload.value) {
    return
  }
  currentPage.value = nextPage
  await loadData()
}

async function changePageSize() {
  currentPage.value = 1
  await loadData()
}

async function selectTrace(traceId: string) {
  selectedTraceId.value = traceId
  await loadTraceDetail(traceId)
}

async function loadTraceDetail(traceId: string) {
  detailLoading.value = true
  detailError.value = ''
  try {
    const detail = await fetchGatewayTraceDetail(traceId)
    if (selectedTraceId.value !== traceId) {
      return
    }
    if (!detail.success || !detail.trace) {
      selectedTrace.value = null
      detailError.value = detail.message ?? 'trace 不存在或已被清理'
      return
    }
    selectedTrace.value = detail.trace
  } catch (err) {
    if (selectedTraceId.value === traceId) {
      selectedTrace.value = null
      detailError.value = err instanceof Error ? err.message : '加载 trace 详情失败'
    }
  } finally {
    if (selectedTraceId.value === traceId) {
      detailLoading.value = false
    }
  }
}

function badgeTone(status?: string | null) {
  if (status === 'error' || status === 'timeout') {
    return 'danger'
  }
  if (status === 'ok') {
    return 'success'
  }
  return 'neutral'
}

function diagnosisTone(status?: string | null) {
  if (status === 'timeout' || status === 'error') {
    return 'danger'
  }
  if (status === 'slow' || status === 'cancelled') {
    return 'warning'
  }
  if (status === 'ok') {
    return 'success'
  }
  return 'neutral'
}

function formatDiagnosisBottleneck(diagnosis?: GatewayTraceDiagnosis | null) {
  if (!diagnosis) {
    return '-'
  }
  if (diagnosis.bottleneck_label && diagnosis.bottleneck_ms !== null && diagnosis.bottleneck_ms !== undefined) {
    return `${diagnosis.bottleneck_label} ${formatDuration(diagnosis.bottleneck_ms)}`
  }
  if (diagnosis.severity === 'timeout') {
    return 'timeout'
  }
  if (diagnosis.severity === 'cancelled') {
    return 'cancelled'
  }
  return '-'
}

function isSlowEvent(event: GatewayTraceEvent) {
  const duration = event.duration_ms ?? 0
  if (event.stage === 'asr_done') {
    return duration > 1500
  }
  if (event.stage === 'llm_done' || event.stage === 'tts_first_audio') {
    return duration > 3000
  }
  if (event.stage === 'llm_tts_done' || event.stage === 'llm_tts_timeout') {
    return duration > 15000
  }
  return false
}

function formatSummary(summary: Record<string, unknown>) {
  const text = JSON.stringify(summary, null, 2)
  const maxLength = 3000
  if (text.length <= maxLength) {
    return text
  }
  return `${text.slice(0, maxLength)}\n... 已截断 ${text.length - maxLength} 个字符`
}

function getEventGapMs(previous: GatewayTraceEvent, current: GatewayTraceEvent) {
  const previousEpoch = Number(previous.ts_epoch)
  const currentEpoch = Number(current.ts_epoch)
  if (Number.isFinite(previousEpoch) && Number.isFinite(currentEpoch)) {
    return Math.max(0, Math.round((currentEpoch - previousEpoch) * 1000 * 10) / 10)
  }
  const previousMs = Date.parse(previous.ts)
  const currentMs = Date.parse(current.ts)
  if (!Number.isFinite(previousMs) || !Number.isFinite(currentMs)) {
    return null
  }
  return Math.max(0, Math.round((currentMs - previousMs) * 10) / 10)
}

function formatDuration(value?: number | null) {
  if (value === null || value === undefined) {
    return '-'
  }
  if (value >= 1000) {
    return `${(value / 1000).toFixed(2)}s`
  }
  return `${value.toFixed(1)}ms`
}

function formatDate(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

onMounted(loadData)
</script>
