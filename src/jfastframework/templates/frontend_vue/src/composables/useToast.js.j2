import { computed, readonly } from 'vue'

import { useNotificationStore } from '@/stores/notification.store.js'

// The queue lives in the notification store, not at module scope here. One
// queue is the requirement -- a toast raised inside a service and a toast
// raised in a component both have to reach the single mounted <ToastHost> --
// and the store gets that plus a view of the queue in devtools.
//
// Everything below resolves the store per call rather than once at import. A
// service module is evaluated before app.use(pinia) runs, so a store captured
// up here would throw "getActivePinia() was called with no active Pinia" the
// moment a service imported this file. The computed is safe for the same
// reason: it is not evaluated until something reads it, which is at render.
const toasts = readonly(computed(() => useNotificationStore().toasts))

const push = (variant, message, options) => useNotificationStore().push(variant, message, options)

const success = (message, options) => push('success', message, options)
const error = (message, options) => push('error', message, options)
const info = (message, options) => push('info', message, options)

const dismiss = (id) => useNotificationStore().dismiss(id)
const clear = () => useNotificationStore().clear()

export function useToast() {
  // Read-only outward: the queue is only ever changed through these functions,
  // because a splice from a component would strand that toast's timer.
  return { toasts, success, error, info, dismiss, clear }
}
