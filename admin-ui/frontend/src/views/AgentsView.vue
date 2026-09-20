<template>
  <section class="page-grid two-column agents-grid">
    <article class="card list-panel agents-list-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Local Agents</p>
          <h3>Agent 能力目录</h3>
        </div>
      </div>

      <div v-if="error" class="inline-error">{{ error }}</div>

      <div v-if="isInitialLoading" class="item-list">
        <div v-for="index in 4" :key="index" class="list-item loading-card">
          <div class="loading-stack">
            <div class="skeleton-line skeleton-line-medium"></div>
            <div class="skeleton-line skeleton-line-short"></div>
          </div>
          <div class="skeleton-pill"></div>
        </div>
      </div>

      <div v-else-if="agents.length === 0" class="empty-state">
        当前数据库里还没有 Agent。重启或 Apply LLM 后会自动发现本地 Agent 文件。
      </div>

      <div v-else class="item-list">
        <button
          v-for="item in agents"
          :key="item.agent_id"
          class="list-item"
          :class="{ active: form.agent_id === item.agent_id }"
          type="button"
          @click="selectAgent(item)"
        >
          <div class="agent-list-copy">
            <strong>{{ item.name }}</strong>
            <p>{{ item.agent_id }}</p>
          </div>
          <StatusBadge :label="item.enabled ? 'enabled' : 'disabled'" :tone="item.enabled ? 'success' : 'danger'" />
        </button>
      </div>
    </article>

    <article class="card form-panel agents-editor-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Editor</p>
          <h3>{{ form.name || form.agent_id || '选择一个 Agent' }}</h3>
        </div>
      </div>

      <div v-if="isInitialLoading" class="editor-form">
        <div v-for="index in 6" :key="index" class="loading-field" :class="{ 'full-width': index >= 3 }">
          <div class="skeleton-line skeleton-line-short"></div>
          <div class="skeleton-input"></div>
        </div>
      </div>

      <form v-else class="editor-form" @submit.prevent="handleSubmit">
        <label>
          <span>agent_id</span>
          <input v-model.trim="form.agent_id" type="text" disabled />
        </label>

        <label>
          <span>名称</span>
          <input v-model.trim="form.name" type="text" disabled />
        </label>

        <label>
          <span>Module</span>
          <input :value="form.module ?? '-'" type="text" disabled />
        </label>

        <label>
          <span>Class</span>
          <input :value="form.class_name ?? '-'" type="text" disabled />
        </label>

        <label class="full-width">
          <span>描述</span>
          <textarea :value="form.description ?? ''" rows="4" disabled />
        </label>

        <fieldset class="full-width checklist-field">
          <legend>触发示例</legend>
          <div v-if="form.trigger_examples_json.length === 0" class="empty-state compact-empty">
            这个 Agent 暂无触发示例。
          </div>
          <div v-else class="chip-list">
            <span v-for="item in form.trigger_examples_json" :key="item" class="chip">{{ item }}</span>
          </div>
        </fieldset>

        <fieldset class="full-width checklist-field">
          <legend>内部能力</legend>
          <div class="chip-list">
            <span v-for="item in form.allowed_tools_json" :key="item" class="chip">tool={{ item }}</span>
            <span v-for="item in form.tool_groups_json" :key="item" class="chip">group={{ item }}</span>
            <span v-if="form.allowed_tools_json.length === 0 && form.tool_groups_json.length === 0" class="chip">
              no tools
            </span>
          </div>
        </fieldset>

        <label class="toggle-field">
          <input v-model="form.enabled" type="checkbox" />
          <span>全局启用该 Agent</span>
        </label>
        <p class="form-hint full-width">禁用 Agent 会在保存时清除所有 Bot 对它的绑定，避免 Apply 时加载到不可用能力。</p>

        <div v-if="error" class="inline-error full-width">{{ error }}</div>
        <div v-if="successMessage" class="inline-success full-width">{{ successMessage }}</div>

        <div class="form-actions full-width">
          <button class="primary-button" :disabled="saving || !form.agent_id" type="submit">
            {{ saving ? '保存中...' : '保存 Agent' }}
          </button>
          <button class="ghost-button" :disabled="saving || !selectedAgent" type="button" @click="resetForm">
            重置
          </button>
        </div>
      </form>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import StatusBadge from '../components/StatusBadge.vue'
import { fetchAgents, saveAgent } from '../lib/api'
import type { AgentItem } from '../types'

type AgentFormState = Omit<AgentItem, 'updated_at'>

const agents = ref<AgentItem[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const successMessage = ref('')
const isInitialLoading = ref(true)
let selectedAgent: AgentItem | null = null

const createEmptyForm = (): AgentFormState => ({
  agent_id: '',
  name: '',
  description: '',
  module: '',
  class_name: '',
  enabled: false,
  enabled_by_default: false,
  trigger_examples_json: [],
  allowed_tools_json: [],
  tool_groups_json: [],
})

const form = reactive<AgentFormState>(createEmptyForm())

function applyForm(item: AgentFormState) {
  Object.assign(form, item)
}

function selectAgent(item: AgentItem) {
  selectedAgent = item
  successMessage.value = ''
  applyForm({
    agent_id: item.agent_id,
    name: item.name,
    description: item.description,
    module: item.module,
    class_name: item.class_name,
    enabled: item.enabled,
    enabled_by_default: item.enabled_by_default,
    trigger_examples_json: [...item.trigger_examples_json],
    allowed_tools_json: [...item.allowed_tools_json],
    tool_groups_json: [...item.tool_groups_json],
  })
}

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const items = await fetchAgents()
    agents.value = items
    const matched = selectedAgent ? items.find((item) => item.agent_id === selectedAgent?.agent_id) : null
    if (matched) {
      selectAgent(matched)
    } else if (items.length > 0) {
      selectAgent(items[0])
    } else {
      selectedAgent = null
      applyForm(createEmptyForm())
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载 Agent 失败'
  } finally {
    loading.value = false
    isInitialLoading.value = false
  }
}

function resetForm() {
  successMessage.value = ''
  if (selectedAgent) {
    selectAgent(selectedAgent)
  }
}

async function handleSubmit() {
  if (!form.agent_id) {
    return
  }
  saving.value = true
  error.value = ''
  successMessage.value = ''
  try {
    const result = await saveAgent({ ...form })
    successMessage.value = `已保存 Agent: ${result.item.agent_id}，记得回到 Overview 执行 Apply / Reload。`
    await loadData()
    selectAgent(result.item)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存 Agent 失败'
  } finally {
    saving.value = false
  }
}

onMounted(loadData)
</script>
