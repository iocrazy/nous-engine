import { create } from 'zustand'

type NotifyPermission = 'default' | 'granted' | 'denied'
type ToastFn = (message: string, type: 'success' | 'error' | 'info') => void

interface NotificationState {
  /** 已发过通知的 task_id —— 去重，避免同一 task 重复弹（轮询 + WS 双触发）。 */
  notified: Set<string>
  /** 浏览器 Notification 权限快照（'default' = 还没问过）。 */
  permission: NotifyPermission

  /** 首次询问浏览器通知权限。拒绝 / 无 API → permission 落 'denied'，降级 toast-only。 */
  requestPermission: () => Promise<void>
  /** 对某 task 发一次完成/失败通知：永远发 app 内 toast；
   * 仅当「权限 granted + 页面失焦」时额外发系统 Notification。已通知过的 task_id 跳过。 */
  notifyOnce: (
    taskId: string,
    message: string,
    type: 'success' | 'error',
    toast: ToastFn,
  ) => void
}

export const useNotificationStore = create<NotificationState>((set, get) => ({
  notified: new Set(),
  permission: 'default',

  requestPermission: async () => {
    // jsdom / 老浏览器无 Notification API → 直接降级。
    if (typeof Notification === 'undefined') {
      set({ permission: 'denied' })
      return
    }
    if (Notification.permission !== 'default') {
      set({ permission: Notification.permission as NotifyPermission })
      return
    }
    try {
      const result = await Notification.requestPermission()
      set({ permission: result as NotifyPermission })
    } catch {
      set({ permission: 'denied' })
    }
  },

  notifyOnce: (taskId, message, type, toast) => {
    const { notified } = get()
    if (notified.has(taskId)) return
    set({ notified: new Set(notified).add(taskId) })

    // app 内 toast —— 永远发，是降级基线（spec §6.6：拒绝权限就只 toast）。
    toast(message, type === 'error' ? 'error' : 'success')

    // 系统通知 —— 仅「权限 granted + 页面失焦」才发（spec §6.3）。
    const canSystemNotify =
      typeof Notification !== 'undefined' &&
      Notification.permission === 'granted' &&
      !document.hasFocus()
    if (canSystemNotify) {
      try {
        new Notification('nous-engine', { body: message })
      } catch {
        // 某些环境构造会抛 —— 静默吞，toast 已经发了。
      }
    }
  },
}))
