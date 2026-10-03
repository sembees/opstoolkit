<template>
  <el-card shadow="never" class="card-section">
    <template v-if="title || icon || $slots.extra" #header>
      <div class="card-section__header">
        <span v-if="title || icon" class="card-section__title-side">
          <el-icon v-if="icon" class="card-section__icon"><component :is="icon" /></el-icon>
          <span v-if="title" class="card-section__title">{{ title }}</span>
        </span>
        <slot name="extra" />
      </div>
    </template>
    <slot />
  </el-card>
</template>

<script setup>
defineProps({
  title: { type: String, default: "" },
  icon: { type: String, default: "" },
})
</script>

<style scoped>
.card-section__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}

.card-section__title-side {
  display: inline-flex;
  align-items: center;
  gap: var(--ot-space-1);
}

.card-section__icon {
  color: var(--ot-text-3);
}
</style>
