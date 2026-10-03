<template>
  <div class="page-header">
    <div class="page-header__main">
      <h2 class="page-header__title">{{ title }}</h2>
      <p v-if="desc" class="page-header__desc">{{ desc }}</p>
    </div>
    <div v-if="$slots.actions" class="page-header__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup>
defineProps({
  title: { type: String, required: true },
  desc: { type: String, default: "" },
})
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--ot-space-4);
  margin-bottom: var(--ot-space-4);
}
.page-header__title {
  font-size: var(--ot-font-xl);
  font-weight: 600;
  color: var(--ot-text-1);
  line-height: 1.4;
  margin: 0;
}
.page-header__desc {
  font-size: var(--ot-font-sm);
  color: var(--ot-text-3);
  margin-top: var(--ot-space-1);
}
.page-header__actions {
  display: flex;
  align-items: center;
  gap: var(--ot-space-2);
}
@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
  }
}
</style>
