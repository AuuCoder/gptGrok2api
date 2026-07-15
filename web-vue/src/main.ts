import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { nanocatZhCN, setNanocatLocale } from 'nanocat-ui'
import router from './router'
import { setUnauthorizedHandler } from './api/client'
import { useAuthStore } from './stores/auth'
import { applyThemeMode, getStoredThemeMode } from './lib/theme'
import App from './App.vue'
import './style.css'
import './styles/features.css'

setNanocatLocale(nanocatZhCN)
applyThemeMode(getStoredThemeMode())

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)

setUnauthorizedHandler(() => {
  const authStore = useAuthStore()
  authStore.clearIdentity()
  void router.replace({ name: 'login' }).catch(() => {})
})

app.use(router)

app.mount('#app')
