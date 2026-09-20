import { createRouter, createWebHistory } from 'vue-router'

import OverviewView from './views/OverviewView.vue'
import BotsView from './views/BotsView.vue'
import RobotsView from './views/RobotsView.vue'
import McpServersView from './views/McpServersView.vue'
import RuntimeView from './views/RuntimeView.vue'
import TracesView from './views/TracesView.vue'
import TTSProfilesView from './views/TTSProfilesView.vue'
import AgentsView from './views/AgentsView.vue'
import LoginView from './views/LoginView.vue'
import { fetchAuthStatus } from './lib/api'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { title: 'Login', public: true } },
    { path: '/', name: 'overview', component: OverviewView, meta: { title: 'Overview' } },
    { path: '/runtime', name: 'runtime', component: RuntimeView, meta: { title: 'Runtime' } },
    { path: '/traces', name: 'traces', component: TracesView, meta: { title: 'Traces' } },
    { path: '/service-settings/gateway', redirect: '/' },
    { path: '/service-settings/tts', redirect: '/tts-profiles' },
    { path: '/tts-profiles', name: 'tts-profiles', component: TTSProfilesView, meta: { title: 'TTS Profiles' } },
    { path: '/bots', name: 'bots', component: BotsView, meta: { title: 'Bot Templates' } },
    { path: '/agents', name: 'agents', component: AgentsView, meta: { title: 'Agents' } },
    { path: '/robots', name: 'robots', component: RobotsView, meta: { title: 'Robots' } },
    { path: '/mcp-servers', name: 'mcp-servers', component: McpServersView, meta: { title: 'MCP Servers' } },
  ],
})

router.beforeEach(async (to) => {
  const status = await fetchAuthStatus().catch(() => ({ authenticated: false }))
  if (to.meta.public) {
    if (to.name === 'login' && status.authenticated) {
      return typeof to.query.redirect === 'string' ? to.query.redirect : '/'
    }
    return true
  }
  if (!status.authenticated) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  return true
})
