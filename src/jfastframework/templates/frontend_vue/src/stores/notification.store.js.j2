import { defineStore } from 'pinia'

// Timer handles are kept out of state on purpose: a setTimeout id is not
// serialisable, and parking one in reactive state puts noise in devtools and in
// any snapshot of the store.
const timers = new Map()

// Unique within one page load, which is all a toast id has to be. A counter
// beats adding a uuid dependency for that.
let nextId = 0

const DURATIONS = {
  success: 4000,
  info: 4000,
  // Errors outlive the rest because they usually name something the reader has
  // to act on, and a sentence gone in four seconds cannot be re-read.
  error: 8000,
}

// A failing poll raises the same toast every few seconds. Without a cap the
// queue, and the DOM rendering it, grows for as long as the tab stays open.
const MAX_VISIBLE = 5

/**
 * Every toast in the app, in one queue.
 *
 * A store rather than state at module scope, because the queue has two kinds of
 * writer: a component, and a service that has no component to live in. Both
 * reach the same array here, and one mounted `<ToastHost>` renders it. The
 * store also puts the queue in devtools, which is where you look when a toast
 * did not appear.
 *
 * Raise toasts through `useToast()` in `@/composables/useToast.js`, not by
 * calling these actions: that is the surface the components already use, and it
 * is the one that stays safe to import from a service.
 */
export const useNotificationStore = defineStore('notification', {
  state: () => ({
    toasts: [],
  }),

  actions: {
    // `duration: 0` opts out of auto-dismiss, for the error that has to be
    // acknowledged rather than waited out.
    push(variant, message, options = {}) {
      const id = nextId
      nextId += 1

      // ?? DURATIONS.info because an unknown variant yields undefined, and
      // setTimeout(fn, undefined) fires on the next tick -- the toast would
      // appear and vanish in the same frame.
      const duration = options.duration ?? DURATIONS[variant] ?? DURATIONS.info
      this.toasts.push({ id, variant, message, title: options.title ?? null })

      // Drop the oldest rather than refuse the newest: the toast worth showing
      // is the one describing what just happened.
      if (this.toasts.length > MAX_VISIBLE) {
        this.dismiss(this.toasts[0].id)
      }

      if (duration > 0) {
        timers.set(id, setTimeout(() => this.dismiss(id), duration))
      }

      return id
    },

    // Clearing the timer is the whole point of keeping the map. A toast
    // dismissed by hand still has a pending setTimeout, and that callback fires
    // later against a queue that has moved on.
    dismiss(id) {
      const timer = timers.get(id)
      if (timer) {
        clearTimeout(timer)
        timers.delete(id)
      }

      this.toasts = this.toasts.filter((toast) => toast.id !== id)
    },

    clear() {
      timers.forEach(clearTimeout)
      timers.clear()
      this.toasts = []
    },
  },
})
