import { onBeforeUnmount, onMounted } from 'vue'

const EXCLUSIVE_FLOATING_MENU_EVENT = 'ai-floating-menu-open'

function createMenuId(prefix: string) {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Math.random().toString(36).slice(2)}`
}

export function useExclusiveFloatingMenu(closeMenu: () => void, prefix = 'floating-menu') {
  const menuId = createMenuId(prefix)

  function announceOpen() {
    if (typeof window === 'undefined') return
    window.dispatchEvent(new CustomEvent(EXCLUSIVE_FLOATING_MENU_EVENT, { detail: menuId }))
  }

  function handleOtherMenuOpen(event: Event) {
    if ((event as CustomEvent<string>).detail === menuId) return
    closeMenu()
  }

  onMounted(() => {
    if (typeof window === 'undefined') return
    window.addEventListener(EXCLUSIVE_FLOATING_MENU_EVENT, handleOtherMenuOpen)
  })

  onBeforeUnmount(() => {
    if (typeof window === 'undefined') return
    window.removeEventListener(EXCLUSIVE_FLOATING_MENU_EVENT, handleOtherMenuOpen)
  })

  return {
    announceOpen,
  }
}
