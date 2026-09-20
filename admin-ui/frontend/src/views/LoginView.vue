<template>
  <main class="login-page">
    <section class="login-card">
      <div class="login-copy-panel">
        <div class="login-brand">
          <span class="brand-mark">SC</span>
          <div>
            <p class="eyebrow">Admin Access</p>
            <h1>Control Deck</h1>
          </div>
        </div>

        <div class="login-hero-copy">
          <p class="login-kicker">Server Config Console</p>
          <h2>进入后台前，先确认操作者身份。</h2>
          <p>
            登录后可以查看运行态、修改 Bot / Robot / MCP 配置，并执行 Apply 切换运行中的 runtime snapshot。
          </p>
        </div>

        <div class="login-signal-grid" aria-label="security notes">
          <div>
            <span>Auth</span>
            <strong>Cookie Session</strong>
          </div>
          <div>
            <span>Scope</span>
            <strong>Admin API</strong>
          </div>
          <div>
            <span>Mode</span>
            <strong>Operator Only</strong>
          </div>
        </div>
      </div>

      <div class="login-form-panel">
        <div class="login-form-heading">
          <p class="eyebrow">Sign In</p>
          <h3>登录后台</h3>
          <p>使用服务端环境变量中配置的 Admin 账号。</p>
        </div>

        <form class="login-form" @submit.prevent="submit">
          <label class="login-field">
            <span>用户名</span>
            <input v-model.trim="username" autocomplete="username" type="text" required />
          </label>

          <label class="login-field">
            <span>密码</span>
            <div class="password-input-wrap">
              <input
                v-model="password"
                autocomplete="current-password"
                :type="showPassword ? 'text' : 'password'"
                required
              />
              <button class="text-button" type="button" @click="showPassword = !showPassword">
                {{ showPassword ? '隐藏' : '显示' }}
              </button>
            </div>
          </label>

          <div v-if="error" class="login-alert">{{ error }}</div>

          <button class="login-submit primary-button" :disabled="loading" type="submit">
            <span>{{ loading ? '正在校验身份' : '进入 Control Deck' }}</span>
            <span class="button-arrow" aria-hidden="true">→</span>
          </button>
        </form>

        <p class="login-footnote">
          生产环境建议开启 HTTPS，并设置 <code>ADMIN_COOKIE_SECURE=true</code>。
        </p>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { loginAdmin } from '../lib/api'

const route = useRoute()
const router = useRouter()
const username = ref('admin')
const password = ref('')
const loading = ref(false)
const error = ref('')
const showPassword = ref(false)

async function submit() {
  loading.value = true
  error.value = ''
  try {
    const result = await loginAdmin(username.value, password.value)
    if (!result.authenticated) {
      throw new Error(result.message ?? '登录失败')
    }
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.push(redirect)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '登录失败'
  } finally {
    loading.value = false
  }
}
</script>
