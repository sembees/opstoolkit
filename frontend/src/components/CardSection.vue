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
.card-section {
  /* 层次感收口：极轻投影（tokens.css --ot-shadow-1，位移 ≤3px、透明度 ≤.06），
     只有「纸面贴着桌面」的一点点分层，没有浮起感；
     EP shadow="never" 本身是零阴影，这里用样式补，比 shadow="always" 的
     --el-box-shadow-light（0 0 12px 大半径）轻得多。 */
  box-shadow: var(--ot-shadow-1);
}

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
