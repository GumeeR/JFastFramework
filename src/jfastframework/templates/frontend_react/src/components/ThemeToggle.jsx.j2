import { useTheme } from '@/hooks/useTheme.js'

const BUTTON =
  'inline-flex size-9 items-center justify-center rounded-lg border border-line text-ink-soft transition-colors hover:bg-elevated hover:text-ink'

// The icon shows the destination, not the current state: a sun means "switch
// to light". Showing the current state is the other common choice and it is
// worse -- a button whose icon does not change when you press it reads as
// broken, and this one changes either way.
export default function ThemeToggle() {
  const { theme, resolved, toggleTheme, followSystem } = useTheme()
  const label = resolved === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'

  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        onClick={toggleTheme}
        aria-label={label}
        title={label}
        className={BUTTON}
      >
        {resolved === 'dark' ? (
          <svg
            className="size-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
          </svg>
        ) : (
          <svg
            className="size-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9" />
          </svg>
        )}
      </button>

      {/* Shown only once the choice is pinned, which is also the only time it
          does anything. It is not optional polish: the button beside it can
          only ever pin light or dark, so without this one nothing in the app
          can reach 'system' again and the first click is permanent. */}
      {theme === 'system' ? null : (
        <button
          type="button"
          onClick={followSystem}
          aria-label="Follow the system theme"
          title="Follow the system theme"
          className={BUTTON}
        >
          <svg
            className="size-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <rect x="2" y="3" width="20" height="14" rx="2" />
            <path d="M8 21h8M12 17v4" />
          </svg>
        </button>
      )}
    </div>
  )
}
