/**
 * Toasts, for components.
 *
 * The list itself lives in `@/stores/notification.store.js`; this is the
 * hook-shaped door onto it. One store, two doors, on purpose: a component
 * subscribes so it re-renders when the list changes, while an axios
 * interceptor or a route loader -- neither of which may call a hook -- imports
 * `toast` and calls it directly. Both write to the same list, so a toast
 * raised deep inside a service and one raised in a component appear together,
 * in order, under the same timers.
 *
 * This file used to hold a second store of its own. Two stores meant a toast
 * raised from a service went into one list and the host rendered the other,
 * which fails silently: no error, just nothing on screen.
 *
 * The default export is the store, so a selector works as usual:
 *
 *     const toasts = useToast((state) => state.toasts)
 *
 * Select one field at a time. A single selector that builds an object returns
 * a new reference every call, and zustand v5 compares with Object.is -- so the
 * component would re-render on every unrelated write.
 *
 * Nothing appears on screen without <ToastHost /> mounted at the app root.
 */
export { useNotificationStore as default, toast, useToast } from '@/stores/notification.store.js'
