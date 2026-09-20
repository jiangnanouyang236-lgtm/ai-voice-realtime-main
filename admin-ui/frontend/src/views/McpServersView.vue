<template>
  <section class="page-grid two-column">
    <article class="card list-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Tool Registry</p>
          <h3>MCP Servers</h3>
        </div>
        <button class="primary-button" @click="startCreate">新建 MCP</button>
      </div>

      <div v-if="error" class="inline-error">{{ error }}</div>

      <div v-if="isInitialLoading" class="item-list">
        <div v-for="index in 4" :key="index" class="list-item loading-card">
          <div class="loading-stack">
            <div class="skeleton-line skeleton-line-medium"></div>
            <div class="skeleton-line skeleton-line-short"></div>
            <div class="skeleton-line skeleton-line-short"></div>
          </div>
          <div class="list-badges">
            <div class="skeleton-pill"></div>
            <div class="skeleton-pill"></div>
          </div>
        </div>
      </div>

      <div v-else class="item-list">
        <button
          v-for="item in servers"
          :key="item.server_key"
          class="list-item"
          :class="{ active: form.server_key === item.server_key && !isCreating }"
          @click="selectServer(item)"
        >
          <div>
            <strong>{{ item.display_name }}</strong>
            <p>{{ item.server_key }}</p>
            <small>{{ item.type }}</small>
          </div>
          <div class="list-badges">
            <StatusBadge :label="item.enabled ? 'enabled' : 'disabled'" :tone="item.enabled ? 'success' : 'danger'" />
            <StatusBadge :label="item.type" tone="neutral" />
          </div>
        </button>
      </div>
    </article>

    <article class="card form-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Connection</p>
          <h3>{{ isCreating ? '新建 MCP Server' : `编辑 ${form.display_name || form.server_key}` }}</h3>
        </div>
      </div>

      <div v-if="isInitialLoading" class="editor-form">
        <div v-for="index in 7" :key="index" class="loading-field" :class="{ 'full-width': index >= 4 }">
          <div class="skeleton-line skeleton-line-short"></div>
          <div class="skeleton-input"></div>
        </div>
      </div>

      <form v-else class="editor-form" @submit.prevent="handleSubmit">
        <label>
          <span>server_key</span>
          <input v-model.trim="form.server_key" :disabled="!isCreating" type="text" required />
        </label>

        <label>
          <span>显示名称</span>
          <input v-model.trim="form.display_name" type="text" required />
        </label>

        <label>
          <span>类型</span>
          <select v-model="form.type">
            <option value="sse">sse</option>
            <option value="streamable_http">streamable_http</option>
          </select>
        </label>

        <label class="toggle-field">
          <input v-model="form.enabled" type="checkbox" />
          <span>启用该 MCP Server</span>
        </label>

        <label class="full-width">
          <span>URL</span>
          <input v-model.trim="form.url" type="url" placeholder="https://example.com/mcp" required />
        </label>

        <fieldset class="full-width checklist-field">
          <legend>Headers</legend>
          <KeyValueEditor v-model="form.headers_json" />
        </fieldset>

        <div class="meta-strip full-width" v-if="!isCreating">
          <div>
            <span>Updated At</span>
            <strong>{{ formatDate(selectedServer?.updated_at) }}</strong>
          </div>
          <div>
            <span>Transport</span>
            <strong>{{ selectedServer?.type ?? '-' }}</strong>
          </div>
        </div>

        <div v-if="error" class="inline-error full-width">{{ error }}</div>
        <div v-if="successMessage" class="inline-success full-width">{{ successMessage }}</div>

        <div class="form-actions full-width">
          <button class="primary-button" :disabled="saving" type="submit">
            {{ saving ? '保存中...' : '保存 MCP Server' }}
          </button>
          <button class="ghost-button" :disabled="saving" type="button" @click="resetForm">重置</button>
          <button
            v-if="!isCreating"
            class="danger-button"
            :disabled="saving || deleting"
            type="button"
            @click="handleDelete"
          >
            {{ deleting ? '删除中...' : '删除 MCP' }}
          </button>
        </div>
      </form>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import KeyValueEditor from '../components/KeyValueEditor.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { deleteMcpServer, fetchMcpServers, saveMcpServer } from '../lib/api'
import type { MCPServerItem } from '../types'

type McpFormState = Omit<MCPServerItem, 'updated_at'>

const servers = ref<MCPServerItem[]>([])
const isCreating = ref(false)
const loading = ref(false)
const saving = ref(false)
const deleting = ref(false)
const error = ref('')
const successMessage = ref('')
const selectedServer = ref<MCPServerItem | null>(null)
const isInitialLoading = ref(true)

const createEmptyForm = (): McpFormState => ({
  server_key: '',
  display_name: '',
  type: 'streamable_http',
  url: '',
  command: null,
  args_json: [],
  headers_json: {},
  enabled: true,
})

const form = reactive<McpFormState>(createEmptyForm())

function applyForm(item: McpFormState) {
  Object.assign(form, item)
}

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const items = await fetchMcpServers()
    servers.value = items
    if (selectedServer.value) {
      const matched = items.find((item) => item.server_key === selectedServer.value?.server_key)
      if (matched) {
        selectServer(matched)
        return
      }
    }
    if (items.length > 0) {
      selectServer(items[0])
    } else {
      startCreate()
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载 MCP Servers 失败'
  } finally {
    loading.value = false
    isInitialLoading.value = false
  }
}

function selectServer(item: MCPServerItem) {
  isCreating.value = false
  selectedServer.value = item
  successMessage.value = ''
  applyForm({
    server_key: item.server_key,
    display_name: item.display_name,
    type: item.type,
    url: item.url ?? '',
    command: null,
    args_json: [],
    headers_json: { ...(item.headers_json ?? {}) },
    enabled: item.enabled,
  })
}

function startCreate() {
  isCreating.value = true
  selectedServer.value = null
  successMessage.value = ''
  applyForm(createEmptyForm())
}

function resetForm() {
  successMessage.value = ''
  if (isCreating.value) {
    applyForm(createEmptyForm())
    return
  }
  if (selectedServer.value) {
    selectServer(selectedServer.value)
  }
}

async function handleSubmit() {
  saving.value = true
  error.value = ''
  successMessage.value = ''
  try {
    const result = await saveMcpServer(
      {
        server_key: form.server_key,
        display_name: form.display_name,
        type: form.type,
        url: form.url || null,
        command: null,
        args_json: [],
        headers_json: form.headers_json,
        enabled: form.enabled,
      },
      isCreating.value,
    )
    successMessage.value = `已保存 MCP Server: ${result.item.server_key}`
    await loadData()
    selectServer(result.item)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存 MCP Server 失败'
  } finally {
    saving.value = false
  }
}

async function handleDelete() {
  if (isCreating.value || !selectedServer.value) {
    return
  }
  const serverKey = selectedServer.value.server_key
  if (!window.confirm(`确认删除 MCP Server「${selectedServer.value.display_name}」(${serverKey}) 吗？它会同时从 Bot 绑定中移除，删除后需要 Apply / Reload 才会切到运行态。`)) {
    return
  }

  deleting.value = true
  error.value = ''
  successMessage.value = ''
  try {
    await deleteMcpServer(serverKey)
    selectedServer.value = null
    await loadData()
    successMessage.value = `已删除 MCP Server: ${serverKey}，记得回到 Overview 执行 Apply / Reload。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除 MCP Server 失败'
  } finally {
    deleting.value = false
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
