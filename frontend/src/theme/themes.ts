/**
 * UI/UX display schemes for the Agentic CAE Platform.
 *
 * Each scheme is implemented purely by remapping the TailwindCSS v4 color
 * variables (`--color-slate-*` and `--color-sky-*`) under an
 * `html[data-theme="<id>"]` selector in index.css. Because every panel already
 * styles surfaces/text with the `slate` neutral ramp and `sky` primary ramp,
 * overriding those variables re-tints the entire application — drawing upload,
 * viewer, chat, results dashboard, and 3D viewer included — without changing
 * any panel markup or its behaviour.
 *
 * Status colors (red/amber) are intentionally left untouched so error and
 * warning semantics stay consistent across every scheme.
 */
export type ThemeId = 'light' | 'forge' | 'slate' | 'ember' | 'sand'

export interface ThemeMeta {
  /** Stable id, written to `html[data-theme]` and persisted in localStorage. */
  id: ThemeId
  /** Human-readable name shown in the switcher. */
  label: string
  /** One-line description of the look and feel. */
  description: string
  /**
   * Three representative swatch colors (background, surface, primary) used to
   * render a small preview chip in the switcher menu.
   */
  swatches: [string, string, string]
}

export const THEMES: ThemeMeta[] = [
  {
    id: 'light',
    label: 'Daylight',
    description: 'Clean light workspace with a sky-blue accent',
    swatches: ['#f1f5f9', '#ffffff', '#0284c7'],
  },
  {
    id: 'forge',
    label: 'Forge',
    description: 'Dark engineering mode with electric green accents',
    swatches: ['#111111', '#1a1a1a', '#76b900'],
  },
  {
    id: 'slate',
    label: 'Slate',
    description: 'Modern dark with a cool teal accent',
    swatches: ['#121212', '#1e1e1e', '#2dd4bf'],
  },
  {
    id: 'ember',
    label: 'Ember',
    description: 'Warm dark with an industrial amber glow',
    swatches: ['#1a1410', '#241e16', '#f59e0b'],
  },
  {
    id: 'sand',
    label: 'Sand',
    description: 'Warm paper tones with an amber accent',
    swatches: ['#f3ede3', '#faf7f2', '#ea580c'],
  },
]

export const DEFAULT_THEME: ThemeId = 'light'
export const THEME_STORAGE_KEY = 'uep.theme'

/** Type guard for persisted/URL values. */
export function isThemeId(value: unknown): value is ThemeId {
  return (
    typeof value === 'string' && THEMES.some((theme) => theme.id === value)
  )
}
