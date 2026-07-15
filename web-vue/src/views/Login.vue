<template>
  <div class="min-h-screen px-4">
    <div class="flex min-h-screen items-center justify-center">
      <div class="w-full max-w-md rounded-[2.5rem] border border-border bg-card p-10 shadow-2xl shadow-black/10">
        <div class="text-center">
          <h1 class="text-3xl font-semibold text-foreground">GPTGrok2API</h1>
          <p class="mt-2 text-sm text-muted-foreground">控制台登录</p>
        </div>

        <form class="mt-8 space-y-6" @submit.prevent="handleLogin">
          <div class="space-y-2">
            <label for="password" class="ui-field-label text-sm font-medium text-foreground">
              管理密钥
            </label>
            <Input
              id="password"
              v-model="password"
              type="password"
              size="md"
              block
              placeholder="输入 Bearer key"
              :disabled="isLoading"
            />
          </div>

          <Button
            type="submit"
            size="md"
            variant="primary"
            block
            :disabled="isLoading || !password"
          >
            {{ isLoading ? '登录中...' : '登录' }}
          </Button>
        </form>

      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Input } from 'nanocat-ui'
import { useToast } from '@/composables/useToast'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const authStore = useAuthStore()
const toast = useToast()

const password = ref('')
const isLoading = ref(false)

async function handleLogin() {
  if (!password.value) return

  isLoading.value = true

  try {
    const loggedIn = await authStore.login(password.value)
    if (!loggedIn) {
      toast.error('密钥无效或已失效。')
      return
    }
    await router.push(authStore.isUser ? { name: 'studio' } : { name: 'dashboard' })
  } catch (error: any) {
    toast.error(error.message || '登录失败，请检查密码。')
  } finally {
    isLoading.value = false
  }
}
</script>
