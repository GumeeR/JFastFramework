import { create } from 'zustand'

/**
 * The toast list, shared by every raiser.
 *
 * Components call `useToast()`; services, axios interceptors and route loaders
 * -- none of which may call a hook -- import `toast` and call it directly.
 * Both write to this one store, so a toast raised deep inside a service and
 * one raised in a component land in the same list, in order, and dismiss under
 * the same timers.
 */
const TIMEOUT = 5000

// An error is the one kind you cannot afford to miss while looking elsewhere.
const ERROR_TIMEOUT = 10000

// Monotonic ids, never the array index: an index is reused as soon as an
// earlier toast is dismissed, which makes React reconcile the wrong node and
// makes a pending timer remove the toast that inherited the slot.
let nextId = 0

// Timer handles are not render state -- keeping them out of the store means a
// toast list that stays JSON-shaped and comparable.
const timers = new Map()

export const useNotificationStore = create((set, get) => ({
  toasts: [],

  push: ({ message, kind = 'info', timeout = TIMEOUT }) => {
    const id = ++nextId
    set((state) => ({ toasts: [...state.toasts, { id, kind, message }] }))
    if (timeout > 0) {
      timers.set(id, setTimeout(() => get().dismiss(id), timeout))
    }
    return id
  },

  dismiss: (id) => {
    clearTimeout(timers.get(id))
    timers.delete(id)
    set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) }))
  },

  clear: () => {
    timers.forEach(clearTimeout)
    timers.clear()
    set({ toasts: [] })
  },
}))

/**
 * The same three calls, for code that has no hooks.
 *
 * `kind` is what the renderer keys off: an icon and a label, not only a
 * colour, since colour alone is not a signal every reader receives.
 */
export const toast = {
  info: (message) => useNotificationStore.getState().push({ message, kind: 'info' }),
  success: (message) => useNotificationStore.getState().push({ message, kind: 'success' }),
  error: (message) =>
    useNotificationStore.getState().push({ message, kind: 'error', timeout: ERROR_TIMEOUT }),
}

export function useToast() {
  // One selector per field on purpose: a single selector building an object
  // returns a new reference every call, and zustand v5 compares with Object.is,
  // so the component would re-render on every unrelated store write.
  const toasts = useNotificationStore((state) => state.toasts)
  const dismiss = useNotificationStore((state) => state.dismiss)
  return { toasts, dismiss, ...toast }
}
