<template>
  <div class="kv-list">
    <div v-for="(item, index) in rows" :key="index" class="kv-row">
      <input v-model="item.key" type="text" placeholder="Header Key" />
      <input v-model="item.value" type="text" placeholder="Header Value 或 ${ENV_NAME}" />
      <button type="button" class="ghost-button" @click="removeRow(index)">删除</button>
    </div>

    <button type="button" class="ghost-button" @click="addRow">新增 Header</button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

type Row = {
  key: string
  value: string
}

const props = defineProps<{
  modelValue: Record<string, string>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, string>]
}>()

const rows = ref<Row[]>([])

function rowsFromModel(value: Record<string, string> | undefined | null): Row[] {
  const entries = Object.entries(value ?? {})
  return entries.length > 0 ? entries.map(([key, item]) => ({ key, value: item })) : [{ key: '', value: '' }]
}

function rowsToModel(value: Row[]): Record<string, string> {
  return value.reduce<Record<string, string>>((acc, item) => {
    const key = item.key.trim()
    if (key) {
      acc[key] = item.value
    }
    return acc
  }, {})
}

function sameRows(left: Row[], right: Row[]) {
  if (left.length !== right.length) {
    return false
  }
  return left.every((item, index) => item.key === right[index]?.key && item.value === right[index]?.value)
}

function sameRecord(left: Record<string, string>, right: Record<string, string> | undefined | null) {
  const rightValue = right ?? {}
  const leftKeys = Object.keys(left)
  const rightKeys = Object.keys(rightValue)
  if (leftKeys.length !== rightKeys.length) {
    return false
  }
  return leftKeys.every((key) => left[key] === rightValue[key])
}

watch(
  () => props.modelValue,
  (value) => {
    const nextRows = rowsFromModel(value)
    if (!sameRows(rows.value, nextRows)) {
      rows.value = nextRows
    }
  },
  { immediate: true, deep: true },
)

watch(
  rows,
  (value) => {
    const normalized = rowsToModel(value)
    if (!sameRecord(normalized, props.modelValue)) {
      emit('update:modelValue', normalized)
    }
  },
  { deep: true },
)

function addRow() {
  rows.value = [...rows.value, { key: '', value: '' }]
}

function removeRow(index: number) {
  const next = rows.value.filter((_, rowIndex) => rowIndex !== index)
  rows.value = next.length > 0 ? next : [{ key: '', value: '' }]
}
</script>
