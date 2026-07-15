<template>
  <div class="table-shell">
    <div class="scrollbar-slim table-shell__scroll">
      <slot />
    </div>
    <div
      v-if="$slots.footer"
      class="table-shell__footer"
      :class="{ 'table-shell__footer--bordered': footerBorder }"
    >
      <slot name="footer" />
    </div>
  </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{
  footerBorder?: boolean
}>(), {
  footerBorder: false,
})
</script>

<style scoped>
.table-shell {
  min-width: 0;
}

.table-shell__scroll {
  overflow-x: auto;
}

.table-shell__footer {
  padding: var(--table-shell-footer-padding, 12px 16px);
}

.table-shell__footer--bordered {
  border-top: 1px solid hsl(var(--border));
}
</style>
