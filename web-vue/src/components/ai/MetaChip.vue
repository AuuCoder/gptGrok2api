<template>
  <NanocatMetaChip
    :tone="nanocatTone"
    :variant="variant"
    :size="resolvedSize"
    :tone-class="resolvedToneClass"
    :chip-class="resolvedChipClass"
  >
    <slot />
  </NanocatMetaChip>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { MetaChip as NanocatMetaChip } from 'nanocat-ui'
import { pillToneClass, type PillTone, type PillVariant } from '@/lib/pillTones'

type MetaChipTone = 'default' | PillTone
type MetaChipSize = 'xs' | 'sm' | 'md'

const sizeClassMap: Record<MetaChipSize, string> = {
  xs: '!min-h-6 !px-2.5 !py-1 !text-[11px]',
  sm: '!min-h-[1.625rem] !px-3 !py-1.5 !text-[11px]',
  md: '!min-h-8 !px-3.5 !py-2 !text-xs',
}

const props = withDefaults(defineProps<{
  tone?: MetaChipTone
  variant?: PillVariant
  size?: MetaChipSize
  strong?: boolean
  chipClass?: string
}>(), {
  tone: 'default',
  variant: 'soft',
  size: 'sm',
  strong: false,
  chipClass: '',
})

const resolvedTone = computed<PillTone>(() => {
  if (props.tone === 'default') return 'muted'
  return props.tone
})

const resolvedSize = computed(() => props.size === 'md' ? 'md' : 'sm')

const nanocatTone = computed(() => {
  if (resolvedTone.value === 'success') return 'success'
  if (resolvedTone.value === 'warning') return 'warning'
  if (resolvedTone.value === 'danger') return 'error'
  if (resolvedTone.value === 'info') return 'info'
  return 'neutral'
})

const resolvedToneClass = computed(() => pillToneClass(resolvedTone.value, props.variant))

const resolvedChipClass = computed(() => {
  const classes = [
    'min-w-0 justify-center tracking-normal',
    sizeClassMap[props.size],
  ]
  if (props.strong) classes.push('!font-semibold')
  if (props.chipClass) classes.push(props.chipClass)
  return classes.join(' ')
})
</script>
