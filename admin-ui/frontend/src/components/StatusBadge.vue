<template>
  <span class="status-badge" :class="toneClass">{{ label }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  label: string
  tone?: 'neutral' | 'success' | 'warning' | 'danger'
}>()

const toneClass = computed(() => `tone-${props.tone ?? 'neutral'}`)
</script>
