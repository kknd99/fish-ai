import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { wsService } from '@/services/websocket'

// Global State
const username = ref<string | null>(localStorage.getItem('auth_username'))
const isLoggedIn = ref(localStorage.getItem('auth_logged_in') === 'true')

export function useAuth() {
  const router = useRouter()

  const isAuthenticated = computed(() => isLoggedIn.value)

  function persist(user: string) {
    username.value = user
    isLoggedIn.value = true
    localStorage.setItem('auth_username', user)
    localStorage.setItem('auth_logged_in', 'true')
  }

  function clearLocalState() {
    username.value = null
    isLoggedIn.value = false
    localStorage.removeItem('auth_username')
    localStorage.removeItem('auth_logged_in')
  }

  function setAuthenticated(user: string) {
    persist(user)
    // 启动 WebSocket 连接
    wsService.start()
  }

  /**
   * 向服务端确认会话是否仍然有效。
   *
   * 真正的凭据是后端下发的 HttpOnly 会话 cookie（前端读不到、也不需要读），
   * 这里的 localStorage 只是用于界面状态。启动时对账一次，避免"本地标记为已登录、
   * 实际 cookie 已过期"时先渲染出面板再被踢回登录页。
   */
  async function verifySession(): Promise<boolean> {
    try {
      const response = await fetch('/auth/session', { credentials: 'same-origin' })
      if (!response.ok) {
        clearLocalState()
        wsService.stop()
        return false
      }
      const data = await response.json().catch(() => ({}))
      persist(typeof data?.username === 'string' && data.username ? data.username : 'admin')
      return true
    } catch (e) {
      console.error('Session verification failed', e)
      clearLocalState()
      wsService.stop()
      return false
    }
  }

  async function logout() {
    // 必须通知服务端清除 HttpOnly cookie，否则"退出登录"只是界面假象
    try {
      await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' })
    } catch (e) {
      console.error('Logout request failed', e)
    }

    clearLocalState()

    // 停止 WebSocket 连接
    wsService.stop()

    // Redirect to login if using router
    if (router) {
      router.push('/login')
    } else {
      window.location.href = '/login'
    }
  }

  async function login(user: string, pass: string): Promise<boolean> {
    try {
      const response = await fetch('/auth/status', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'same-origin',
        body: JSON.stringify({ username: user, password: pass }),
      })

      if (response.ok) {
        setAuthenticated(user)
        return true
      } else {
        return false
      }
    } catch (e) {
      console.error('Login error', e)
      return false
    }
  }

  return {
    username,
    isAuthenticated,
    login,
    logout,
    verifySession,
  }
}
