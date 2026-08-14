import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { ThemeContext } from './ThemeContext'
import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  isThemeId,
  type ThemeId,
} from './themes'

/**
 * Reads the persisted scheme (falling back to the default) without touching
 * the DOM, so it is safe to call during the initial render.
 */
function readStoredTheme(): ThemeId {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    if (isThemeId(stored)) {
      return stored
    }
  } catch {
    // localStorage may be unavailable (private mode / SSR); ignore.
  }
  return DEFAULT_THEME
}

/**
 * Provides the active UI/UX display scheme to the app and applies it by
 * setting `html[data-theme]`. The scheme is persisted to localStorage so a
 * user's choice survives reloads.
 *
 * Applying the theme at the document root (rather than a wrapper element) means
 * the CSS-variable overrides in index.css cascade to every rendered panel and
 * to the page background, giving each scheme a consistent, app-wide identity.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeId>(readStoredTheme)

  // Apply the active theme to the document root.
  useEffect(() => {
    document.documentElement.dataset.theme = theme
  }, [theme])

  const setTheme = useCallback((next: ThemeId) => {
    setThemeState(next)
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      // Persisting is best-effort; the in-memory value still applies.
    }
  }, [])

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}
