import { createContext, useContext } from 'react'
import type { ThemeId } from './themes'

export interface ThemeContextValue {
  /** The currently active scheme id. */
  theme: ThemeId
  /** Switch to a different scheme (persisted to localStorage). */
  setTheme: (theme: ThemeId) => void
}

export const ThemeContext = createContext<ThemeContextValue | null>(null)

/**
 * Access the active display scheme and setter. Must be used within a
 * <ThemeProvider>.
 */
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return ctx
}
