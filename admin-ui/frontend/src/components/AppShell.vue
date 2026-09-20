<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <p class="eyebrow">Server Config</p>
        <h1>Control Deck</h1>
        <p class="brand-copy">统一管理 Bot 模板、Robot 绑定与 MCP 连接。</p>
      </div>

      <nav class="nav">
        <RouterLink v-for="item in navItems" :key="item.to" :to="item.to" class="nav-link">
          <span class="nav-label">{{ item.label }}</span>
          <span class="nav-hint">{{ item.hint }}</span>
        </RouterLink>
      </nav>
    </aside>

    <main class="content">
      <header class="topbar">
        <div>
          <p class="eyebrow">Phase 2</p>
          <h2>{{ route.meta.title ?? 'Server Config Admin' }}</h2>
        </div>
        <div class="topbar-actions">
          <p class="topbar-copy">保存配置写入数据库，Apply 才会切换运行中的 runtime snapshot。</p>
          <button class="ghost-button compact-button" :disabled="loggingOut" type="button" @click="handleLogout">
            {{ loggingOut ? '退出中...' : '退出登录' }}
          </button>
        </div>
      </header>

      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { logoutAdmin } from '../lib/api'

const route = useRoute()
const router = useRouter()
const loggingOut = ref(false)
const navItems = [
  { to: '/', label: 'Overview', hint: '状态与 Apply' },
  { to: '/runtime', label: 'Runtime', hint: 'Gateway 会话与 Robot 现场' },
  { to: '/traces', label: 'Traces', hint: '语音链路黑匣子' },
  { to: '/bots', label: 'Bot Templates', hint: 'Prompt / 模型 / TTS' },
  { to: '/tts-profiles', label: 'TTS Profiles', hint: '音色与 Provider' },
  { to: '/agents', label: 'Agents', hint: '本地 Agent 启停' },
  { to: '/robots', label: 'Robots', hint: 'robot_id 与 Bot 绑定' },
  { to: '/mcp-servers', label: 'MCP Servers', hint: '远程工具配置与状态' },
]

async function handleLogout() {
  loggingOut.value = true
  try {
    await logoutAdmin()
  } finally {
    loggingOut.value = false
    router.push('/login')
  }
}
</script>
