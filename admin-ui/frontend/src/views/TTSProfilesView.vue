<template>
  <section class="page-grid two-column">
    <article class="card list-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Profiles</p>
          <h3>TTS 配置</h3>
        </div>
        <button class="primary-button" type="button" @click="startCreate">新建 TTS</button>
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

      <div v-else-if="profiles.length === 0" class="empty-state">
        当前没有 TTS Profile。
      </div>

      <div v-else class="item-list">
        <button
          v-for="item in profiles"
          :key="item.tts_id"
          class="list-item"
          :class="{ active: form.tts_id === item.tts_id && !isCreating }"
          type="button"
          @click="selectProfile(item)"
        >
          <div>
            <strong>{{ item.tts_name }}</strong>
            <p>{{ item.tts_id }}</p>
          </div>
          <div class="list-badges">
            <StatusBadge :label="providerLabel(item.provider_type)" tone="neutral" />
            <StatusBadge :label="item.enabled ? 'enabled' : 'disabled'" :tone="item.enabled ? 'success' : 'danger'" />
          </div>
        </button>
      </div>
    </article>

    <article class="card form-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Editor</p>
          <h3>{{ isCreating ? '新建 TTS Profile' : `编辑 ${form.tts_name || form.tts_id}` }}</h3>
        </div>
      </div>

      <div v-if="isInitialLoading" class="editor-form">
        <div v-for="index in 7" :key="index" class="loading-field" :class="{ 'full-width': index >= 5 }">
          <div class="skeleton-line skeleton-line-short"></div>
          <div class="skeleton-input"></div>
        </div>
      </div>

      <form v-else class="editor-form" @submit.prevent="handleSubmit">
        <label>
          <span>tts_id</span>
          <input v-model.trim="form.tts_id" :disabled="!isCreating" type="text" required />
        </label>

        <label>
          <span>名称</span>
          <input v-model.trim="form.tts_name" type="text" required />
        </label>

        <fieldset class="full-width checklist-field">
          <legend>Provider</legend>
          <div class="provider-tabs">
            <button
              v-for="provider in providerTypes"
              :key="provider"
              class="ghost-button compact-button"
              :class="{ selected: form.provider_type === provider }"
              type="button"
              @click="selectProvider(provider)"
            >
              {{ providerLabel(provider) }}
            </button>
          </div>
        </fieldset>

        <label>
          <span>Speed</span>
          <input v-model.number="form.speed" type="number" min="0.5" max="2" step="0.1" required />
        </label>

        <label class="toggle-field">
          <input v-model="form.enabled" type="checkbox" />
          <span>启用该 Profile</span>
        </label>

        <template v-if="form.provider_type === 'qwen3_custom_voice'">
          <label>
            <span>Voice</span>
            <select v-model="form.voice" required>
              <option v-for="voice in options.tts_custom_voices" :key="voice" :value="voice">
                {{ voice }}
              </option>
              <option
                v-if="form.voice && !options.tts_custom_voices.includes(form.voice)"
                :value="form.voice"
              >
                {{ form.voice }}
              </option>
            </select>
          </label>

          <label class="full-width">
            <span>Instruct</span>
            <textarea v-model="form.instruct" rows="5" required />
          </label>
        </template>

        <template v-else>
          <label class="full-width">
            <span>Path</span>
            <input v-model.trim="form.path" type="text" placeholder="/base/leijun-6s.mp3" required />
          </label>

          <label class="full-width">
            <span>Content</span>
            <textarea v-model="form.content" rows="5" required />
          </label>
        </template>

        <div v-if="error" class="inline-error full-width">{{ error }}</div>
        <div v-if="successMessage" class="inline-success full-width">{{ successMessage }}</div>

        <div class="form-actions full-width">
          <button class="primary-button" :disabled="saving" type="submit">
            {{ saving ? '保存中...' : '保存 TTS Profile' }}
          </button>
          <button class="ghost-button" :disabled="saving" type="button" @click="resetForm">重置</button>
          <button
            v-if="!isCreating"
            class="danger-button"
            :disabled="saving || deleting"
            type="button"
            @click="handleDelete"
          >
            {{ deleting ? '删除中...' : '删除 TTS Profile' }}
          </button>
        </div>
      </form>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import StatusBadge from '../components/StatusBadge.vue'
import { deleteTtsProfile, fetchOptions, fetchTtsProfiles, saveTtsProfile } from '../lib/api'
import type { OptionsPayload, TTSProfileItem, TTSProviderType } from '../types'

type TTSProfileFormState = Omit<TTSProfileItem, 'updated_at'>

const DEFAULT_TTS_PROFILE_ID = 'default_tts_profile'
const DEFAULT_INSTRUCT = '自然、温柔、稳定、口语化，语速适中，情绪轻微，不夸张。'
const providerTypes: TTSProviderType[] = ['qwen3_custom_voice', 'qwen3_base']

const profiles = ref<TTSProfileItem[]>([])
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

let lastSelected: TTSProfileItem | null = null

function providerLabel(provider: TTSProviderType) {
  return provider === 'qwen3_custom_voice' ? 'CustomVoice' : 'Base'
}

function createEmptyForm(provider: TTSProviderType = 'qwen3_custom_voice'): TTSProfileFormState {
  return {
    tts_id: '',
    tts_name: '',
    provider_type: provider,
    speed: 1.0,
    enabled: true,
    voice: provider === 'qwen3_custom_voice' ? options.tts_custom_voices[0] ?? 'serena' : null,
    instruct: provider === 'qwen3_custom_voice' ? DEFAULT_INSTRUCT : null,
    path: provider === 'qwen3_base' ? '/base/leijun-6s.mp3' : null,
    content: provider === 'qwen3_base' ? '' : null,
  }
}

const form = reactive<TTSProfileFormState>(createEmptyForm())

function applyForm(item: TTSProfileFormState) {
  Object.assign(form, item)
}

function selectProvider(provider: TTSProviderType) {
  form.provider_type = provider
  if (provider === 'qwen3_custom_voice') {
    form.voice = form.voice || options.tts_custom_voices[0] || 'serena'
    form.instruct = form.instruct || DEFAULT_INSTRUCT
    form.path = null
    form.content = null
    return
  }
  form.voice = null
  form.instruct = null
  form.path = form.path || '/base/leijun-6s.mp3'
  form.content = form.content || ''
}

function selectProfile(item: TTSProfileItem) {
  isCreating.value = false
  lastSelected = item
  successMessage.value = ''
  applyForm({
    tts_id: item.tts_id,
    tts_name: item.tts_name,
    provider_type: item.provider_type,
    speed: item.speed,
    enabled: item.enabled,
    voice: item.voice ?? null,
    instruct: item.instruct ?? null,
    path: item.path ?? null,
    content: item.content ?? null,
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
    selectProfile(lastSelected)
  }
}

function buildPayload(): TTSProfileFormState {
  const basePayload: TTSProfileFormState = {
    tts_id: form.tts_id.trim(),
    tts_name: form.tts_name.trim(),
    provider_type: form.provider_type,
    speed: Number(form.speed),
    enabled: form.enabled,
    voice: null,
    instruct: null,
    path: null,
    content: null,
  }
  if (form.provider_type === 'qwen3_custom_voice') {
    basePayload.voice = form.voice?.trim() || null
    basePayload.instruct = form.instruct?.trim() || null
  } else {
    basePayload.path = form.path?.trim() || null
    basePayload.content = form.content?.trim() || null
  }
  return basePayload
}

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [profileItems, optionItems] = await Promise.all([fetchTtsProfiles(), fetchOptions()])
    profiles.value = profileItems
    Object.assign(options, optionItems)

    if (lastSelected) {
      const matched = profileItems.find((item) => item.tts_id === lastSelected?.tts_id)
      if (matched) {
        selectProfile(matched)
        return
      }
    }

    const defaultProfile = profileItems.find((item) => item.tts_id === DEFAULT_TTS_PROFILE_ID)
    if (defaultProfile) {
      selectProfile(defaultProfile)
    } else if (profileItems.length > 0) {
      selectProfile(profileItems[0])
    } else {
      startCreate()
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载 TTS Profile 失败'
  } finally {
    loading.value = false
    isInitialLoading.value = false
  }
}

async function handleSubmit() {
  saving.value = true
  error.value = ''
  successMessage.value = ''
  try {
    const result = await saveTtsProfile(buildPayload(), isCreating.value)
    successMessage.value = `已保存 TTS Profile: ${result.item.tts_id}`
    await loadData()
    selectProfile(result.item)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存 TTS Profile 失败'
  } finally {
    saving.value = false
  }
}

async function handleDelete() {
  if (isCreating.value || !lastSelected) {
    return
  }
  const ttsId = lastSelected.tts_id
  if (!window.confirm(`确认删除 TTS Profile「${lastSelected.tts_name}」(${ttsId}) 吗？`)) {
    return
  }

  deleting.value = true
  error.value = ''
  successMessage.value = ''
  try {
    await deleteTtsProfile(ttsId)
    lastSelected = null
    await loadData()
    successMessage.value = `已删除 TTS Profile: ${ttsId}，记得回到 Overview 执行 Apply / Reload。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除 TTS Profile 失败'
  } finally {
    deleting.value = false
  }
}

onMounted(loadData)
</script>
