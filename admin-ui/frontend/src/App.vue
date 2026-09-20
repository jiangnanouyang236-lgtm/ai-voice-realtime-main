<template>
  <RouterView v-if="route.meta.public" />
  <AppShell v-else />
</template>

<script setup lang="ts">
import { RouterView, useRoute } from 'vue-router'

import AppShell from './components/AppShell.vue'

const route = useRoute()
</script>
