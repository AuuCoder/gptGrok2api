<template>
  <div ref="rootRef" class="floating-action-menu" :style="rootStyle">
    <Button
      v-if="triggerVariant === 'button'"
      variant="outline"
      :size="size === 'xs' ? 'xs' : 'sm'"
      :disabled="disabled"
      :root-class="triggerRootClass"
      :aria-expanded="open"
      @click.stop="toggleMenu"
    >
      <span class="truncate">{{ label }}</span>
      <svg
        viewBox="0 0 20 20"
        class="h-3.5 w-3.5 transition-transform"
        :class="open ? 'rotate-180' : ''"
        fill="currentColor"
        aria-hidden="true"
      >
        <path d="M5 7l5 6 5-6H5z" />
      </svg>
    </Button>
    <button
      v-else
      type="button"
      class="ui-input-sm ui-select-trigger floating-action-menu-input-trigger"
      :class="inputTriggerClass"
      :disabled="disabled"
      :aria-expanded="open"
      @click.stop="toggleMenu"
    >
      <span>{{ label }}</span>
      <svg
        viewBox="0 0 20 20"
        class="h-4 w-4 transition-transform"
        :class="open ? 'rotate-180' : ''"
        fill="currentColor"
        aria-hidden="true"
      >
        <path d="M5 7l5 6 5-6H5z" />
      </svg>
    </button>

    <Teleport to="body">
      <div
        v-if="open && !disabled"
        ref="menuRef"
        class="floating-action-menu-panel ui-floating-panel fixed z-[1000]"
        :class="panelClass"
        :style="menuStyle"
        @click.stop
      >
        <template v-for="item in items" :key="item.key">
          <div
            v-if="item.dividerBefore"
            class="floating-menu-divider"
            role="separator"
            aria-hidden="true"
          />
          <div
            v-if="item.heading"
            class="floating-action-menu-heading"
          >
            {{ item.label }}
          </div>
          <button
            v-else-if="item.children?.length"
            type="button"
            class="floating-action-menu-item floating-action-menu-item-parent ui-menu-item"
            :class="[
              item.danger ? 'floating-action-menu-item-danger ui-menu-item-danger' : '',
              activeParentKey === item.key ? 'floating-action-menu-item-active' : '',
            ]"
            :disabled="item.disabled"
            @mouseenter="openSubmenu(item, $event)"
            @focusin="openSubmenu(item, $event)"
            @click="openSubmenu(item, $event)"
          >
            <span>{{ item.label }}</span>
            <svg
              viewBox="0 0 20 20"
              class="h-3.5 w-3.5 transition-transform"
              :class="activeParentKey === item.key ? 'translate-x-0.5' : ''"
              fill="none"
              stroke="currentColor"
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-width="2"
              aria-hidden="true"
            >
              <path d="M8 5l5 5-5 5" />
            </svg>
          </button>
          <button
            v-else
            type="button"
            class="floating-action-menu-item ui-menu-item"
            :class="[
              item.danger ? 'floating-action-menu-item-danger ui-menu-item-danger' : '',
              item.active ? 'floating-action-menu-item-active' : '',
            ]"
            :disabled="item.disabled"
            @mouseenter="closeSubmenu"
            @focusin="closeSubmenu"
            @click="selectItem(item)"
          >
            <span class="floating-action-menu-label">{{ item.label }}</span>
            <svg
              v-if="item.active"
              aria-hidden="true"
              viewBox="0 0 20 20"
              class="floating-action-menu-check h-3.5 w-3.5"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M5 10.5l3 3 7-7" />
            </svg>
          </button>
        </template>
      </div>

      <div
        v-if="open && !disabled && activeChildren.length"
        ref="submenuRef"
        class="floating-action-menu-panel floating-action-submenu-panel ui-floating-panel fixed z-[1001]"
        :class="panelClass"
        :style="submenuStyle"
        @click.stop
      >
        <template v-for="child in activeChildren" :key="child.key">
          <div
            v-if="child.dividerBefore"
            class="floating-menu-divider"
            role="separator"
            aria-hidden="true"
          />
          <div
            v-if="child.heading"
            class="floating-action-menu-heading"
          >
            {{ child.label }}
          </div>
          <button
            v-else
            type="button"
            class="floating-action-menu-item floating-action-menu-item-child ui-menu-item"
            :class="[
              child.danger ? 'floating-action-menu-item-danger ui-menu-item-danger' : '',
              child.active ? 'floating-action-menu-item-active' : '',
            ]"
            :disabled="child.disabled"
            @click="selectItem(child)"
          >
            <span class="floating-action-menu-label">{{ child.label }}</span>
            <svg
              v-if="child.active"
              aria-hidden="true"
              viewBox="0 0 20 20"
              class="floating-action-menu-check h-3.5 w-3.5"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M5 10.5l3 3 7-7" />
            </svg>
          </button>
        </template>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Button } from 'nanocat-ui'
import { useExclusiveFloatingMenu } from '@/composables/useExclusiveFloatingMenu'
import type { ActionMenuItem, UiSize } from 'nanocat-ui'
import type { CSSProperties } from 'vue'

type FloatingActionMenuItem = ActionMenuItem & {
  active?: boolean
  heading?: boolean
  children?: FloatingActionMenuItem[]
}
type FloatingMenuPlacement = 'auto' | 'top' | 'bottom' | 'left' | 'right'
type FloatingMenuTriggerVariant = 'button' | 'input'

const props = withDefaults(defineProps<{
  label: string
  items: FloatingActionMenuItem[]
  disabled?: boolean
  align?: 'left' | 'right'
  placement?: FloatingMenuPlacement
  size?: UiSize
  triggerVariant?: FloatingMenuTriggerVariant
  triggerClass?: string
  menuClass?: string
  menuMinWidth?: number
  triggerMinWidth?: number
  triggerWidth?: number
}>(), {
  disabled: false,
  align: 'right',
  placement: 'auto',
  size: 'sm',
  triggerVariant: 'button',
  triggerClass: '',
  menuClass: 'min-w-max',
})

const emit = defineEmits<{
  (e: 'select', key: string): void
}>()

const rootRef = ref<HTMLElement | null>(null)
const menuRef = ref<HTMLElement | null>(null)
const submenuRef = ref<HTMLElement | null>(null)
const open = ref(false)
const activeParentKey = ref('')
const menuPosition = ref({ left: 0, top: 0, minWidth: 0, maxHeight: 0 })
const submenuPosition = ref({ left: 0, top: 0, minWidth: 0, maxHeight: 0 })
const { announceOpen } = useExclusiveFloatingMenu(closeMenu, 'floating-menu')
let activeParentElement: HTMLElement | null = null
const hasTriggerSizing = computed(() => Boolean(props.triggerWidth || props.triggerMinWidth))
const triggerRootClass = computed(() => [
  'floating-action-menu-trigger justify-between gap-2',
  open.value ? 'floating-action-menu-trigger-active' : '',
  hasTriggerSizing.value ? 'w-full' : '',
  props.triggerClass,
].filter(Boolean).join(' '))
const inputTriggerClass = computed(() => [
  'floating-action-menu-trigger',
  open.value ? 'floating-action-menu-trigger-active' : '',
  hasTriggerSizing.value ? 'w-full' : '',
  props.triggerClass,
].filter(Boolean).join(' '))
const panelClass = computed(() => props.menuClass)
const activeParentItem = computed(() => props.items.find((item) => item.key === activeParentKey.value))
const activeChildren = computed(() => activeParentItem.value?.children || [])
const rootStyle = computed<CSSProperties>(() => {
  const style: CSSProperties = {}
  if (props.triggerWidth) {
    style.width = `${props.triggerWidth}px`
  } else if (props.triggerMinWidth) {
    style.minWidth = `${props.triggerMinWidth}px`
  }
  return style
})

const menuStyle = computed<CSSProperties>(() => {
  const minWidth = Math.max(menuPosition.value.minWidth, props.menuMinWidth || 0)

  return {
    left: `${menuPosition.value.left}px`,
    top: `${menuPosition.value.top}px`,
    minWidth: `${minWidth}px`,
    width: 'max-content',
    maxWidth: 'min(20rem, calc(100vw - 1rem))',
    maxHeight: menuPosition.value.maxHeight ? `${menuPosition.value.maxHeight}px` : undefined,
  }
})

const submenuStyle = computed<CSSProperties>(() => ({
  left: `${submenuPosition.value.left}px`,
  top: `${submenuPosition.value.top}px`,
  minWidth: `${submenuPosition.value.minWidth}px`,
  width: 'max-content',
  maxWidth: 'min(18rem, calc(100vw - 1rem))',
  maxHeight: submenuPosition.value.maxHeight ? `${submenuPosition.value.maxHeight}px` : undefined,
}))

function closeMenu() {
  open.value = false
  closeSubmenu()
}

async function toggleMenu() {
  if (props.disabled) return
  open.value = !open.value
  if (open.value) {
    announceOpen()
    await nextTick()
    updatePosition()
    requestAnimationFrame(updatePosition)
  }
}

function closeSubmenu() {
  activeParentKey.value = ''
  activeParentElement = null
}

async function openSubmenu(item: FloatingActionMenuItem, event?: Event) {
  if (item.disabled || !item.children?.length) return
  activeParentElement = event?.currentTarget instanceof HTMLElement ? event.currentTarget : activeParentElement
  activeParentKey.value = item.key
  await nextTick()
  updateSubmenuPosition()
}

function selectItem(item: FloatingActionMenuItem) {
  if (item.disabled) return
  closeMenu()
  emit('select', item.key)
}

function updatePosition() {
  const root = rootRef.value
  const menu = menuRef.value
  if (!root || !menu) return

  const rect = root.getBoundingClientRect()
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight
  const margin = 8
  const gap = 8
  const menuWidth = Math.max(menu.offsetWidth || 0, rect.width)
  const menuHeight = menu.offsetHeight || 0
  const availableDown = Math.max(0, viewportHeight - margin - rect.bottom - gap)
  const availableUp = Math.max(0, rect.top - margin - gap)
  const availableRight = Math.max(0, viewportWidth - margin - rect.right - gap)
  const availableLeft = Math.max(0, rect.left - margin - gap)
  const placement = resolvePlacement(props.placement, {
    menuWidth,
    menuHeight,
    availableDown,
    availableUp,
    availableRight,
    availableLeft,
  })
  const verticalLeft = props.align === 'left' ? rect.left : rect.right - menuWidth
  const horizontalTop = rect.top
  const maxLeft = Math.max(margin, viewportWidth - margin - menuWidth)
  const maxTop = Math.max(margin, viewportHeight - margin - menuHeight)
  let left = Math.min(maxLeft, Math.max(margin, verticalLeft))
  let top = rect.bottom + gap
  let maxHeight = availableDown

  if (placement === 'top') {
    top = rect.top - gap - menuHeight
    maxHeight = availableUp
  } else if (placement === 'left') {
    left = rect.left - gap - menuWidth
    top = horizontalTop
    maxHeight = viewportHeight - margin * 2
  } else if (placement === 'right') {
    left = rect.right + gap
    top = horizontalTop
    maxHeight = viewportHeight - margin * 2
  }

  left = Math.min(maxLeft, Math.max(margin, left))
  top = Math.min(maxTop, Math.max(margin, top))

  menuPosition.value = {
    left,
    top,
    minWidth: rect.width,
    maxHeight: Math.max(96, Math.floor(maxHeight)),
  }

  if (activeParentKey.value) {
    void nextTick(updateSubmenuPosition)
  }
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
): Exclude<FloatingMenuPlacement, 'auto'> {
  if (placement !== 'auto') return placement
  if (space.menuHeight <= space.availableDown) return 'bottom'
  if (space.menuHeight <= space.availableUp) return 'top'
  if (space.menuWidth <= space.availableRight) return 'right'
  if (space.menuWidth <= space.availableLeft) return 'left'
  return space.availableDown >= space.availableUp ? 'bottom' : 'top'
}

function updateSubmenuPosition() {
  const anchor = activeParentElement
  const submenu = submenuRef.value
  if (!anchor || !anchor.isConnected || !submenu || activeChildren.value.length === 0) return

  const rect = anchor.getBoundingClientRect()
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight
  const menuRect = menuRef.value?.getBoundingClientRect()
  const margin = 8
  const gap = 6
  const submenuWidth = Math.max(submenu.offsetWidth || 0, rect.width)
  const submenuHeight = submenu.offsetHeight || 0
  const rightEdge = Math.max(rect.right, menuRect?.right || rect.right)
  const leftEdge = Math.min(rect.left, menuRect?.left || rect.left)
  const rightLeft = rightEdge + gap
  const left = rightLeft + submenuWidth <= viewportWidth - margin
    ? rightLeft
    : Math.max(margin, leftEdge - gap - submenuWidth)
  const rawTop = rect.top
  const top = Math.max(margin, Math.min(rawTop, viewportHeight - margin - submenuHeight))
  const maxHeight = Math.max(96, Math.floor(viewportHeight - margin - top))

  submenuPosition.value = {
    left,
    top,
    minWidth: rect.width,
    maxHeight,
  }
}

function handleDocumentClick(event: MouseEvent) {
  const target = event.target as Node | null
  if (!target) return
  if (rootRef.value?.contains(target) || menuRef.value?.contains(target) || submenuRef.value?.contains(target)) return
  closeMenu()
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeMenu()
}

onMounted(() => {
  document.addEventListener('click', handleDocumentClick)
  window.addEventListener('resize', updatePosition)
  window.addEventListener('scroll', updatePosition, true)
  document.addEventListener('keydown', handleKeydown)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', handleDocumentClick)
  window.removeEventListener('resize', updatePosition)
  window.removeEventListener('scroll', updatePosition, true)
  document.removeEventListener('keydown', handleKeydown)
})
</script>

<style scoped>
.floating-action-menu {
  position: relative;
  display: inline-flex;
}

.floating-action-menu-input-trigger {
  flex: 0 0 auto;
  white-space: nowrap;
}

.floating-action-menu-panel {
  padding: 6px;
  border: 1px solid hsl(var(--border));
  border-radius: 14px;
  background: hsl(var(--card));
  box-shadow: var(--shadow-floating);
  overflow-y: auto;
  overscroll-behavior: contain;
}

.floating-action-menu-item {
  width: 100%;
  justify-content: flex-start;
  white-space: nowrap;
  text-align: left;
  border-radius: 999px;
}

.floating-action-menu-heading {
  display: flex;
  min-height: 1.35rem;
  align-items: center;
  padding: 0 0.55rem;
  color: hsl(var(--muted-foreground) / 0.7);
  font-size: 0.72rem;
  font-weight: 500;
  line-height: 1;
}

.floating-action-menu-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.floating-action-menu-check {
  margin-left: 1rem;
  flex: 0 0 auto;
}

.floating-action-menu-item-parent {
  justify-content: space-between;
  gap: 12px;
}

.floating-action-menu-item-active {
  background: hsl(var(--accent));
  color: hsl(var(--accent-foreground));
}

.floating-action-submenu-panel {
  overflow-y: auto;
  overscroll-behavior: contain;
}

.floating-action-menu-item-child {
  font-size: 12px;
}

.floating-action-menu-item:disabled {
  opacity: 0.48;
}

.floating-action-menu-item-danger {
  color: hsl(var(--tone-error-foreground));
}

.floating-menu-divider {
  height: 0;
  margin: 4px 8px;
  flex-shrink: 0;
  border-top: 1px solid hsl(var(--border) / 0.82);
}
</style>
