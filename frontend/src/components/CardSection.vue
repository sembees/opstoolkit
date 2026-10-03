<template>
  <el-card shadow="never" class="card-section">
    <template v-if="title || $slots.extra" #header>
      <div class="card-section__header">
        <span v-if="title" class="card-section__title">{{ title }}</span>
        <slot name="extra" />
      </div>
    </template>
    <slot />
  </el-card>
</template>

<script setup>
defineProps({
  title: { type: String, default: "" },
})
</script>

<style scoped>
.card-section__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}
</style>
