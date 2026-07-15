<template>
  <div
    ref="root"
    class="relative"
    :class="block ? 'block w-full' : 'inline-block'"
  >
    <button
      ref="trigger"
      type="button"
      class="ui-input-sm ui-select-trigger grouped-select-trigger"
      :class="block ? 'grouped-select-trigger--block' : ''"
      :title="currentLabel"
      :aria-label="ariaLabel || currentLabel"
      :aria-expanded="open"
      :disabled="disabled"
      @click="toggle"
    >
      <span class="truncate">{{ currentLabel }}</span>
      <svg
        aria-hidden="true"
        viewBox="0 0 20 20"
        class="h-4 w-4 transition-transform"
        :class="open ? 'rotate-180' : ''"
        fill="currentColor"
      >
        <path d="M5 7l5 6 5-6H5z" />
      </svg>
    </button>
  </div>

  <Teleport to="body">
    <div
      v-if="open"
      ref="menu"
      class="ui-floating-panel grouped-select-menu fixed z-[1000]"
      :style="menuStyle"
    >
      <div v-for="group in resolvedGroups" :key="groupKey(group)" class="grouped-select-section">
        <div
          v-if="showGroupLabels && group.label"
          class="grouped-select-heading"
          :class="`grouped-select-heading--${groupLabelAlign}`"
        >
          <i v-if="groupLabelAlign !== 'left'" aria-hidden="true" />
          <span>{{ group.label }}</span>
          <i v-if="groupLabelAlign !== 'right'" aria-hidden="true" />
        </div>
        <button
          v-for="option in group.options"
          :key="option.value"
          type="button"
          class="ui-menu-item grouped-select-option"
          :class="[
            isSelected(option.value) ? 'bg-accent text-accent-foreground' : 'text-muted-foreground',
            option.disabled ? 'cursor-not-allowed opacity-50' : '',
          ]"
          :disabled="option.disabled"
          @click="select(option.value)"
        >
          <span>{{ option.label }}</span>
          <svg
            v-if="isSelected(option.value) && selectedIndicator === 'check'"
            aria-hidden="true"
            viewBox="0 0 20 20"
            class="h-3.5 w-3.5"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M5 10.5l3 3 7-7" />
          </svg>
        </button>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useExclusiveFloatingMenu } from '@/composables/useExclusiveFloatingMenu'

type GroupedSelectOption = {
  label: string
  value: string
  disabled?: boolean
}

type GroupedSelectGroup = {
  label?: string
  options: GroupedSelectOption[]
}
type FloatingMenuPlacement = 'auto' | 'top' | 'bottom' | 'left' | 'right' | 'up' | 'down'

const props = withDefaults(defineProps<{
  modelValue: string | string[]
  groups?: GroupedSelectGroup[]
  options?: GroupedSelectOption[]
  multiple?: boolean
  placeholder?: string
  disabled?: boolean
  ariaLabel?: string
  maxVisibleLabels?: number
  selectedCountText?: string
  selectedIndicator?: 'check' | 'none'
  showGroupLabels?: boolean
  groupLabelAlign?: 'left' | 'center' | 'right'
  placement?: FloatingMenuPlacement
  block?: boolean
}>(), {
  multiple: false,
  placeholder: '请选择',
  disabled: false,
  ariaLabel: '',
  maxVisibleLabels: 3,
  selectedCountText: '项',
  selectedIndicator: 'check',
  showGroupLabels: true,
  groupLabelAlign: 'left',
  placement: 'auto',
  block: false,
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | string[]): void
}>()

const open = ref(false)
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLElement | null>(null)
const menuPosition = ref({ top: 0, left: 0, width: 0, maxHeight: 0 })
const { announceOpen } = useExclusiveFloatingMenu(closeMenu, 'grouped-select')

const resolvedGroups = computed<GroupedSelectGroup[]>(() => {
  if (props.groups?.length) return props.groups
  if (props.options?.length) return [{ options: props.options }]
  return []
})

const flatOptions = computed(() => resolvedGroups.value.flatMap((group) => group.options))

const currentLabel = computed(() => {
  if (props.multiple) {
    const values = Array.isArray(props.modelValue) ? props.modelValue : []
    if (!values.length) return props.placeholder
    if (values.length <= props.maxVisibleLabels) {
      return values
        .map((value) => flatOptions.value.find((option) => option.value === value)?.label || value)
        .join(' / ')
    }
    return `${values.length} ${props.selectedCountText}`
  }

  const rawValue = props.modelValue == null ? '' : String(props.modelValue)
  const match = flatOptions.value.find((option) => option.value === rawValue)
  if (match) return match.label
  return rawValue || props.placeholder
})

const menuStyle = computed(() => ({
  top: `${menuPosition.value.top}px`,
  left: `${menuPosition.value.left}px`,
  minWidth: `${menuPosition.value.width}px`,
  maxHeight: menuPosition.value.maxHeight ? `${menuPosition.value.maxHeight}px` : undefined,
}))

function groupKey(group: GroupedSelectGroup) {
  return group.label || group.options.map((option) => option.value).join('|')
}

function updateMenuPosition() {
  if (!trigger.value) return

  const rect = trigger.value.getBoundingClientRect()
  const spacing = 8
  const padding = 8

  const menuWidth = menu.value?.offsetWidth || rect.width
  const menuHeight = menu.value?.offsetHeight || 0
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight
  const availableDown = Math.max(0, viewportHeight - padding - rect.bottom - spacing)
  const availableUp = Math.max(0, rect.top - padding - spacing)
  const availableRight = Math.max(0, viewportWidth - padding - rect.right - spacing)
  const availableLeft = Math.max(0, rect.left - padding - spacing)
  const placement = resolvePlacement(props.placement, {
    menuWidth,
    menuHeight,
    availableDown,
    availableUp,
    availableRight,
    availableLeft,
  })
  const maxLeft = Math.max(padding, viewportWidth - menuWidth - padding)
  const maxTop = Math.max(padding, viewportHeight - menuHeight - padding)
  let left = rect.left
  let top = rect.bottom + spacing
  let maxHeight = availableDown

  if (placement === 'top') {
    top = rect.top - menuHeight - spacing
    maxHeight = availableUp
  } else if (placement === 'left') {
    left = rect.left - menuWidth - spacing
    top = rect.top
    maxHeight = viewportHeight - padding * 2
  } else if (placement === 'right') {
    left = rect.right + spacing
    top = rect.top
    maxHeight = viewportHeight - padding * 2
  }

  left = Math.min(Math.max(left, padding), maxLeft)
  top = Math.min(Math.max(top, padding), maxTop)

  menuPosition.value = { top, left, width: rect.width, maxHeight: Math.max(96, Math.floor(maxHeight)) }
}

function normalizePlacement(placement: FloatingMenuPlacement) {
  if (placement === 'up') return 'top'
  if (placement === 'down') return 'bottom'
  return placement
}

function resolvePlacement(
  placement: FloatingMenuPlacement,
  space: {
    menuWidth: number
    menuHeight: number
    availableDown: number
    availableUp: number
    availableRight: number
    availableLeft: number
  },
) {
  const normalized = normalizePlacement(placement)
  if (normalized !== 'auto') return normalized
  if (space.menuHeight <= space.availableDown) return 'bottom'
  if (space.menuHeight <= space.availableUp) return 'top'
  if (space.menuWidth <= space.availableRight) return 'right'
  if (space.menuWidth <= space.availableLeft) return 'left'
  return space.availableDown >= space.availableUp ? 'bottom' : 'top'
}

async function openMenu() {
  open.value = true
  announceOpen()
  await nextTick()
  updateMenuPosition()
  requestAnimationFrame(updateMenuPosition)
}

function closeMenu() {
  open.value = false
}

function toggle() {
  if (props.disabled) return
  if (open.value) {
    closeMenu()
    return
  }
  void openMenu()
}

function isSelected(value: string) {
  if (props.multiple) {
    return Array.isArray(props.modelValue) && props.modelValue.includes(value)
  }
  return props.modelValue === value
}

function select(value: string) {
  const option = flatOptions.value.find((item) => item.value === value)
  if (option?.disabled) return

  if (props.multiple) {
    const current = Array.isArray(props.modelValue) ? props.modelValue : []
    const exists = current.includes(value)
    emit('update:modelValue', exists ? current.filter((item) => item !== value) : [...current, value])
    return
  }

  emit('update:modelValue', value)
  closeMenu()
}

function handleClickOutside(event: MouseEvent) {
  const target = event.target as Node
  if (root.value?.contains(target)) return
  if (menu.value?.contains(target)) return
  closeMenu()
}

function handleViewportChange() {
  if (!open.value) return
  updateMenuPosition()
}

function handleKeyDown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeMenu()
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  document.addEventListener('keydown', handleKeyDown)
  window.addEventListener('resize', handleViewportChange)
  window.addEventListener('scroll', handleViewportChange, true)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', handleClickOutside)
  document.removeEventListener('keydown', handleKeyDown)
  window.removeEventListener('resize', handleViewportChange)
  window.removeEventListener('scroll', handleViewportChange, true)
})
</script>

<style scoped>
.grouped-select-trigger {
  width: auto;
}

.grouped-select-trigger--block {
  width: 100%;
}

.grouped-select-menu {
  max-height: min(30rem, calc(100vh - 16px));
  overflow-y: auto;
  overscroll-behavior: contain;
}

.grouped-select-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.grouped-select-section + .grouped-select-section {
  margin-top: 6px;
}

.grouped-select-option {
  white-space: nowrap;
}

.grouped-select-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 18px;
  padding: 2px 6px 0;
  color: hsl(var(--muted-foreground) / 0.6);
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
}

.grouped-select-heading--center {
  justify-content: center;
}

.grouped-select-heading--right {
  justify-content: flex-end;
}

.grouped-select-heading i {
  height: 1px;
  flex: 1 1 auto;
  background: hsl(var(--border));
}
</style>
