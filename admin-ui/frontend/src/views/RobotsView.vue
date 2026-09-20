<template>
  <section class="page-grid two-column">
    <article class="card list-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Robots</p>
          <h3>接入实例</h3>
        </div>
        <button class="primary-button" @click="startCreate">新建 Robot</button>
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
            <div class="skeleton-pill skeleton-pill-short"></div>
          </div>
        </div>
      </div>

      <div v-else class="item-list">
        <button
          v-for="item in robots"
          :key="item.robot_id"
          class="list-item"
          :class="{ active: form.robot_id === item.robot_id && !isCreating }"
          @click="selectRobot(item)"
        >
          <div>
            <strong>{{ item.name }}</strong>
            <p>{{ item.robot_id }}</p>
            <small>绑定 Bot: {{ item.assigned_bot_id }}</small>
          </div>
          <div class="list-badges">
            <StatusBadge :label="item.enabled ? 'enabled' : 'disabled'" :tone="item.enabled ? 'success' : 'danger'" />
            <StatusBadge :label="item.secret_configured ? 'secret' : 'no secret'" :tone="item.secret_configured ? 'success' : 'warning'" />
            <StatusBadge v-if="item.is_new" label="new" tone="warning" />
          </div>
        </button>
      </div>
    </article>

    <article class="card form-panel">
      <div class="section-header">
        <div>
          <p class="eyebrow">Binding</p>
          <h3>{{ isCreating ? '新建 Robot' : `编辑 ${form.name || form.robot_id}` }}</h3>
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
          <span>robot_id</span>
          <input v-model.trim="form.robot_id" :disabled="!isCreating" type="text" required />
        </label>

        <label>
          <span>名称</span>
          <input v-model.trim="form.name" type="text" required />
        </label>

        <label>
          <span>绑定 Bot</span>
          <select v-model="form.assigned_bot_id" required>
            <option v-for="item in botOptions" :key="item.value" :value="item.value">
              {{ item.label }} ({{ item.value }})
            </option>
          </select>
        </label>

        <label>
          <span>Client Type</span>
          <input v-model.trim="form.client_type" list="client-type-options" type="text" placeholder="python / rust" />
          <datalist id="client-type-options">
            <option value="python" />
            <option value="rust" />
          </datalist>
        </label>

        <label class="toggle-field">
          <input v-model="form.enabled" type="checkbox" />
          <span>启用该 Robot</span>
        </label>

        <label class="toggle-field">
          <input v-model="form.is_new" type="checkbox" />
          <span>标记为新接入</span>
        </label>

        <label class="full-width">
          <span>备注</span>
          <textarea v-model="form.notes" rows="6" placeholder="可记录部署位置、用途、现场备注等" />
        </label>

        <div class="meta-strip full-width" v-if="!isCreating">
          <div>
            <span>Last Connected</span>
            <strong>{{ formatDate(selectedRobot?.last_connected_at) }}</strong>
          </div>
          <div>
            <span>Updated At</span>
            <strong>{{ formatDate(selectedRobot?.updated_at) }}</strong>
          </div>
          <div>
            <span>Secret</span>
            <strong>{{ selectedRobot?.secret_configured ? 'configured' : 'not set' }}</strong>
          </div>
          <div>
            <span>Secret Updated</span>
            <strong>{{ formatDate(selectedRobot?.robot_secret_updated_at) }}</strong>
          </div>
        </div>

        <div v-if="!isCreating" class="secret-panel full-width">
          <div>
            <strong>Robot Secret</strong>
            <p>
              Secret 只会在重置后显示一次。客户端后续通过 <code>ROBOT_SECRET</code> 环境变量携带它完成注册认证。
            </p>
          </div>
          <button class="ghost-button" :disabled="saving || resettingSecret" type="button" @click="handleResetSecret">
            {{ resettingSecret ? '生成中...' : selectedRobot?.secret_configured ? '重置 Secret' : '生成 Secret' }}
          </button>
        </div>

        <div v-if="generatedSecret" class="secret-reveal full-width">
          <div>
            <span>请立即保存到客户端环境变量</span>
            <code>ROBOT_SECRET={{ generatedSecret }}</code>
          </div>
          <button class="ghost-button compact-button" type="button" @click="copySecret">
            {{ copiedSecret ? '已复制' : '复制' }}
          </button>
        </div>

        <div v-if="error" class="inline-error full-width">{{ error }}</div>
        <div v-if="successMessage" class="inline-success full-width">{{ successMessage }}</div>

        <div class="form-actions full-width">
          <button class="primary-button" :disabled="saving" type="submit">
            {{ saving ? '保存中...' : '保存 Robot' }}
          </button>
          <button class="ghost-button" :disabled="saving" type="button" @click="resetForm">重置</button>
          <button
            v-if="!isCreating"
            class="danger-button"
            :disabled="saving || resettingSecret || deleting"
            type="button"
            @click="handleDelete"
          >
            {{ deleting ? '删除中...' : '删除 Robot' }}
          </button>
        </div>
      </form>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import StatusBadge from '../components/StatusBadge.vue'
import { deleteRobot, fetchOptions, fetchRobots, resetRobotSecret, saveRobot } from '../lib/api'
import type { OptionsPayload, RobotItem } from '../types'

type RobotFormState = Omit<RobotItem, 'last_connected_at' | 'updated_at' | 'secret_configured' | 'robot_secret_updated_at'>

const robots = ref<RobotItem[]>([])
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
const error = ref('')
const successMessage = ref('')
const selectedRobot = ref<RobotItem | null>(null)
const isInitialLoading = ref(true)
const resettingSecret = ref(false)
const deleting = ref(false)
const generatedSecret = ref('')
const copiedSecret = ref(false)

const createEmptyForm = (): RobotFormState => ({
  robot_id: '',
  name: '',
  assigned_bot_id: options.bots[0]?.value ?? '',
  client_type: '',
  enabled: true,
  is_new: true,
  notes: '',
})

const form = reactive<RobotFormState>(createEmptyForm())

const botOptions = computed(() => options.bots)

function applyForm(item: RobotFormState) {
  Object.assign(form, item)
}

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [robotItems, optionItems] = await Promise.all([fetchRobots(), fetchOptions()])
    robots.value = robotItems
    Object.assign(options, optionItems)
    if (selectedRobot.value) {
      const matched = robotItems.find((item) => item.robot_id === selectedRobot.value?.robot_id)
      if (matched) {
        selectRobot(matched)
        return
      }
    }
    if (robotItems.length > 0) {
      selectRobot(robotItems[0])
    } else {
      startCreate()
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载 Robot 失败'
  } finally {
    loading.value = false
    isInitialLoading.value = false
  }
}

function selectRobot(item: RobotItem) {
  isCreating.value = false
  selectedRobot.value = item
  successMessage.value = ''
  generatedSecret.value = ''
  copiedSecret.value = false
  applyForm({
    robot_id: item.robot_id,
    name: item.name,
    assigned_bot_id: item.assigned_bot_id,
    client_type: item.client_type ?? '',
    enabled: item.enabled,
    is_new: item.is_new,
    notes: item.notes ?? '',
  })
}

function startCreate() {
  isCreating.value = true
  selectedRobot.value = null
  successMessage.value = ''
  generatedSecret.value = ''
  copiedSecret.value = false
  applyForm(createEmptyForm())
}

function resetForm() {
  successMessage.value = ''
  if (isCreating.value) {
    applyForm(createEmptyForm())
    return
  }
  if (selectedRobot.value) {
    selectRobot(selectedRobot.value)
  }
}

async function handleSubmit() {
  saving.value = true
  error.value = ''
  successMessage.value = ''
  try {
    const result = await saveRobot(
      {
        robot_id: form.robot_id,
        name: form.name,
        assigned_bot_id: form.assigned_bot_id,
        client_type: form.client_type || null,
        enabled: form.enabled,
        is_new: form.is_new,
        notes: form.notes || null,
      },
      isCreating.value,
    )
    successMessage.value = `已保存 Robot: ${result.item.robot_id}`
    await loadData()
    selectRobot(result.item)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存 Robot 失败'
  } finally {
    saving.value = false
  }
}

async function handleResetSecret() {
  if (!selectedRobot.value) {
    return
  }
  resettingSecret.value = true
  error.value = ''
  successMessage.value = ''
  copiedSecret.value = false
  try {
    const result = await resetRobotSecret(selectedRobot.value.robot_id)
    successMessage.value = result.message
    await loadData()
    generatedSecret.value = result.robot_secret
  } catch (err) {
    error.value = err instanceof Error ? err.message : '重置 Robot Secret 失败'
  } finally {
    resettingSecret.value = false
  }
}

async function handleDelete() {
  if (isCreating.value || !selectedRobot.value) {
    return
  }
  const robotId = selectedRobot.value.robot_id
  if (!window.confirm(`确认删除 Robot「${selectedRobot.value.name}」(${robotId}) 吗？删除后需要 Apply / Reload 才会切到运行态。`)) {
    return
  }

  deleting.value = true
  error.value = ''
  successMessage.value = ''
  generatedSecret.value = ''
  copiedSecret.value = false
  try {
    await deleteRobot(robotId)
    selectedRobot.value = null
    await loadData()
    successMessage.value = `已删除 Robot: ${robotId}，记得回到 Overview 执行 Apply / Reload。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除 Robot 失败'
  } finally {
    deleting.value = false
  }
}

async function copySecret() {
  if (!generatedSecret.value) {
    return
  }
  await navigator.clipboard.writeText(generatedSecret.value)
  copiedSecret.value = true
}

function formatDate(value?: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

onMounted(loadData)
</script>
