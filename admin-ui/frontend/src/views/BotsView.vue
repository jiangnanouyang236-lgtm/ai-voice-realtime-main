<template>
  <section class="page-grid two-column">
    <article class="card list-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Templates</p>
          <h3>Bot 模板</h3>
        </div>
        <button class="primary-button" @click="startCreate">新建 Bot</button>
      </div>

      <div v-if="error" class="inline-error">{{ error }}</div>

      <div v-if="isInitialLoading" class="item-list">
        <div v-for="index in 4" :key="index" class="list-item loading-card">
          <div class="loading-stack">
            <div class="skeleton-line skeleton-line-medium"></div>
            <div class="skeleton-line skeleton-line-short"></div>
          </div>
          <div class="list-badges">
            <div class="skeleton-pill"></div>
            <div class="skeleton-pill skeleton-pill-short"></div>
          </div>
        </div>
      </div>

      <div v-else class="item-list">
        <button
          v-for="item in bots"
          :key="item.bot_id"
          class="list-item"
          :class="{ active: form.bot_id === item.bot_id && !isCreating }"
          @click="selectBot(item)"
        >
          <div>
            <strong>{{ item.name }}</strong>
            <p>{{ item.bot_id }}</p>
          </div>
          <div class="list-badges">
            <StatusBadge :label="item.enabled ? 'enabled' : 'disabled'" :tone="item.enabled ? 'success' : 'danger'" />
            <StatusBadge v-if="item.is_default" label="default" tone="warning" />
          </div>
        </button>
      </div>
    </article>

    <article class="card form-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Editor</p>
          <h3>{{ isCreating ? '新建 Bot 模板' : `编辑 ${form.name || form.bot_id}` }}</h3>
        </div>
      </div>

      <div v-if="isInitialLoading" class="editor-form">
        <div v-for="index in 8" :key="index" class="loading-field" :class="{ 'full-width': index === 3 || index >= 7 }">
          <div class="skeleton-line skeleton-line-short"></div>
          <div class="skeleton-input"></div>
        </div>
      </div>

      <form v-else class="editor-form" @submit.prevent="handleSubmit">
        <label>
          <span>bot_id</span>
          <input v-model.trim="form.bot_id" :disabled="!isCreating" type="text" required />
        </label>

        <label>
          <span>名称</span>
          <input v-model.trim="form.name" type="text" required />
        </label>

        <label class="full-width">
          <span>System Prompt</span>
          <textarea v-model="form.system_prompt" rows="10" required />
        </label>

        <label>
          <span>Model</span>
          <input v-model.trim="form.model" list="bot-model-options" type="text" required />
          <datalist id="bot-model-options">
            <option v-for="model in options.models" :key="model" :value="model" />
          </datalist>
        </label>

        <label>
          <span>Max Tokens</span>
          <input v-model.number="form.max_tokens" type="number" min="1" required />
        </label>

        <label>
          <span>Max Response Chars</span>
          <input v-model.number="form.max_response_chars" type="number" min="0" required />
          <p class="form-hint">0 表示不限制；正数会在进入 TTS 前硬限制本轮回复字符数。</p>
        </label>

        <label>
          <span>Temperature</span>
          <input v-model.number="form.temperature" type="number" min="0" max="2" step="0.1" required />
        </label>

        <label class="full-width">
          <span>TTS Profile</span>
          <select v-model="form.tts_profile_id" required>
            <option v-for="profile in options.tts_profiles" :key="profile.value" :value="profile.value">
              {{ profile.label }}
            </option>
            <option
              v-if="options.tts_profiles.length === 0 || !options.tts_profiles.some((item) => item.value === form.tts_profile_id)"
              :value="form.tts_profile_id"
            >
              {{ form.tts_profile_id }}
            </option>
          </select>
          <p class="form-hint">Bot 只绑定 TTS Profile ID；Provider、音色、参考音频等细节在 TTS Profiles 页面维护。</p>
        </label>

        <fieldset class="full-width checklist-field">
          <legend>MCP Servers</legend>
          <p class="form-hint">未勾选时不会向该 Bot 暴露任何 MCP 工具。</p>
          <div class="check-grid">
            <label v-for="item in options.enabled_mcp_servers" :key="item.value" class="check-item">
              <input v-model="form.mcp_servers" type="checkbox" :value="item.value" />
              <span>{{ item.label }}</span>
              <small>{{ item.value }}</small>
            </label>
          </div>
        </fieldset>

        <fieldset class="full-width checklist-field">
          <legend>Agents</legend>
          <p class="form-hint">未勾选时不会触发任何 Agent；新增 Agent 需要先在 Agents 页面启用。</p>
          <div class="check-grid">
            <label v-for="item in options.enabled_agents" :key="item.value" class="check-item">
              <input v-model="form.agents" type="checkbox" :value="item.value" />
              <span>{{ item.label }}</span>
              <small>{{ item.value }}</small>
            </label>
          </div>
        </fieldset>

        <label class="toggle-field">
          <input v-model="form.enabled" type="checkbox" />
          <span>启用该 Bot</span>
        </label>

        <label class="toggle-field">
          <input v-model="form.is_default" type="checkbox" />
          <span>设为默认 Bot</span>
        </label>

        <div v-if="error" class="inline-error full-width">{{ error }}</div>
        <div v-if="successMessage" class="inline-success full-width">{{ successMessage }}</div>

        <div class="form-actions full-width">
          <button class="primary-button" :disabled="saving" type="submit">
            {{ saving ? '保存中...' : '保存 Bot' }}
          </button>
          <button class="ghost-button" :disabled="saving" type="button" @click="resetForm">重置</button>
          <button
            v-if="!isCreating"
            class="danger-button"
            :disabled="saving || deleting"
            type="button"
            @click="handleDelete"
          >
            {{ deleting ? '删除中...' : '删除 Bot' }}
          </button>
        </div>
      </form>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import StatusBadge from '../components/StatusBadge.vue'
import { deleteBot, fetchBots, fetchOptions, saveBot } from '../lib/api'
import type { BotItem, OptionsPayload } from '../types'

type BotFormState = Omit<BotItem, 'updated_at'>

const bots = ref<BotItem[]>([])
const options = reactive<OptionsPayload>({
  success: true,
  models: [],
  tts_custom_voices: [],
  tts_profiles: [],
  mcp_types: [],
  enabled_mcp_servers: [],
  enabled_agents: [],
  bots: [],
})
const isCreating = ref(false)
const loading = ref(false)
const saving = ref(false)
const deleting = ref(false)
const error = ref('')
const successMessage = ref('')
const isInitialLoading = ref(true)

const createEmptyForm = (): BotFormState => ({
  bot_id: '',
  name: '',
  system_prompt: '',
  model: options.models[0] ?? 'qwen3-5-9b',
  temperature: 0.7,
  max_tokens: 2000,
  max_response_chars: 0,
  tts_profile_id: options.tts_profiles[0]?.value ?? 'default_tts_profile',
  enabled: true,
  is_default: false,
  mcp_servers: [],
  agents: [],
})

const form = reactive<BotFormState>(createEmptyForm())
let lastSelected: BotItem | null = null

function applyForm(item: BotFormState) {
  Object.assign(form, item)
}

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [botItems, optionItems] = await Promise.all([fetchBots(), fetchOptions()])
    bots.value = botItems
    Object.assign(options, optionItems)

    if (lastSelected) {
      const matched = botItems.find((item) => item.bot_id === lastSelected?.bot_id)
      if (matched) {
        selectBot(matched)
        return
      }
    }

    if (botItems.length > 0) {
      selectBot(botItems[0])
    } else {
      startCreate()
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载 Bot 失败'
  } finally {
    loading.value = false
    isInitialLoading.value = false
  }
}

function selectBot(item: BotItem) {
  isCreating.value = false
  lastSelected = item
  successMessage.value = ''
  applyForm({
    bot_id: item.bot_id,
    name: item.name,
    system_prompt: item.system_prompt,
    model: item.model,
    temperature: item.temperature,
    max_tokens: item.max_tokens,
    max_response_chars: item.max_response_chars,
    tts_profile_id: item.tts_profile_id,
    enabled: item.enabled,
    is_default: item.is_default,
    mcp_servers: [...item.mcp_servers],
    agents: [...item.agents],
  })
}

function startCreate() {
  isCreating.value = true
  lastSelected = null
  successMessage.value = ''
  applyForm(createEmptyForm())
}

function resetForm() {
  successMessage.value = ''
  if (isCreating.value) {
    applyForm(createEmptyForm())
    return
  }
  if (lastSelected) {
    selectBot(lastSelected)
  }
}

async function handleSubmit() {
  saving.value = true
  error.value = ''
  successMessage.value = ''
  try {
    const payload = { ...form, mcp_servers: [...form.mcp_servers], agents: [...form.agents] }
    const result = await saveBot(payload, isCreating.value)
    successMessage.value = `已保存 Bot: ${result.item.bot_id}`
    await loadData()
    selectBot(result.item)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存 Bot 失败'
  } finally {
    saving.value = false
  }
}

async function handleDelete() {
  if (isCreating.value || !lastSelected) {
    return
  }
  const botId = lastSelected.bot_id
  if (!window.confirm(`确认删除 Bot「${lastSelected.name}」(${botId}) 吗？删除后需要 Apply / Reload 才会切到运行态。`)) {
    return
  }

  deleting.value = true
  error.value = ''
  successMessage.value = ''
  try {
    await deleteBot(botId)
    lastSelected = null
    await loadData()
    successMessage.value = `已删除 Bot: ${botId}，记得回到 Overview 执行 Apply / Reload。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除 Bot 失败'
  } finally {
    deleting.value = false
  }
}

onMounted(loadData)
</script>
