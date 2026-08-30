import BaseIcon from '@/components/BaseIcon.jsx'

const VARIANTS = {
  primary: 'border border-transparent bg-brand-600 text-white hover:bg-brand-700',
  outline:
    'border border-line text-ink hover:bg-elevated',
  ghost:
    'border border-transparent text-ink-soft hover:bg-elevated hover:text-ink',
  // Outlined, not solid. Solid brand already means "this is the action to
  // take", so a solid red delete reads as the recommended path.
  danger:
    'border border-brand-600 bg-transparent text-brand-600 hover:bg-brand-600 hover:text-white dark:border-brand-500 dark:text-brand-400',
}

const SIZES = {
  sm: 'h-8 gap-1.5 px-3 text-sm',
  md: 'h-10 gap-2 px-4 text-sm',
}

const BASE =
  'inline-flex items-center justify-center rounded-md font-medium transition-colors duration-150 focus-visible:ring-2 focus-visible:ring-brand-600 focus-visible:ring-offset-2 focus-visible:ring-offset-surface focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50'

// An icon with no `children` gives the button no accessible name -- pass
// aria-label through the rest props in that case.
export default function BaseButton({
  variant = 'primary',
  size = 'md',
  type = 'button',
  icon = null,
  loading = false,
  disabled = false,
  className = '',
  children,
  ...rest
}) {
  return (
    <button
      // Spread first so the guarantees below win: a caller cannot hand back a
      // clickable button while it is loading, or turn a plain button into a
      // form submit by accident.
      {...rest}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`${BASE} ${VARIANTS[variant] ?? VARIANTS.primary} ${SIZES[size] ?? SIZES.md} ${className}`}
    >
      {loading ? <Spinner /> : icon ? <BaseIcon path={icon} size="sm" /> : null}
      {children}
    </button>
  )
}

function Spinner() {
  return (
    <svg className="h-4 w-4 animate-spin motion-reduce:animate-none" viewBox="0 0 24 24" aria-hidden="true">
      <circle
        cx="12"
        cy="12"
        r="10"
        fill="none"
        stroke="currentColor"
        strokeWidth="3"
        className="opacity-25"
      />
      <path
        d="M12 2a10 10 0 0 1 10 10"
        fill="none"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  )
}
