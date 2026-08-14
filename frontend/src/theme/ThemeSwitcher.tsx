import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTheme } from './ThemeContext'
import { THEMES } from './themes'

/**
 * A small preview of a scheme's palette: three overlapping swatch dots
 * (background, surface, primary).
 */
function Swatches({ colors }: { colors: [string, string, string] }) {
  return (
    <span className="flex shrink-0 items-center" aria-hidden="true">
      {colors.map((color, i) => (
        <span
          key={color + i}
          className="h-3.5 w-3.5 rounded-full border border-black/10 shadow-sm"
          style={{ backgroundColor: color, marginLeft: i === 0 ? 0 : -5 }}
        />
      ))}
    </span>
  )
}

/**
 * Display-scheme switcher for the top-right of the app header.
 *
 * Lets the user flip between the UI/UX schemes (Daylight, CAE Platform, Midnight,
 * Blueprint, Sand). The choice is applied app-wide and persisted, and never
 * affects the underlying drawing/chat/optimization/3D functionality — only the
 * presentation.
 *
 * The dropdown menu is rendered through a portal to `document.body` with fixed
 * positioning. This switcher is mounted inside another popover (the profile
 * menu); rendering the menu inline made its lower rows overflow past that
 * popover and sit beneath the main content's stacking contexts, so the bottom
 * options (Blueprint/Sand) were visually obscured and unclickable. Portaling
 * to the body with a high z-index guarantees every option is on top and
 * clickable regardless of ancestor overflow/stacking.
 */
function ThemeSwitcher() {
  const { theme, setTheme } = useTheme()
  const [open, setOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [menuPos, setMenuPos] = useState<{ top: number; right: number }>({
    top: 0,
    right: 0,
  })

  const active = THEMES.find((t) => t.id === theme) ?? THEMES[0]

  // Position the portaled menu just below the trigger, right-edge aligned.
  useLayoutEffect(() => {
    if (!open || !buttonRef.current) return
    const rect = buttonRef.current.getBoundingClientRect()
    setMenuPos({
      top: rect.bottom + 8,
      right: Math.max(8, window.innerWidth - rect.right),
    })
  }, [open])

  // Close on outside click, Escape, scroll, or resize. The menu lives in a
  // portal (outside the trigger's DOM subtree), so the outside-click test must
  // check BOTH the trigger and the menu — otherwise the closing mousedown would
  // fire before an option's click and swallow the selection.
  useEffect(() => {
    if (!open) return
    function onPointerDown(event: MouseEvent) {
      const target = event.target as Node
      if (
        buttonRef.current?.contains(target) ||
        menuRef.current?.contains(target)
      ) {
        return
      }
      setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    function onReflow() {
      setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    window.addEventListener('resize', onReflow)
    // Capture scrolls on any ancestor so the fixed menu doesn't detach visually.
    window.addEventListener('scroll', onReflow, true)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('resize', onReflow)
      window.removeEventListener('scroll', onReflow, true)
    }
  }, [open])

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Display scheme: ${active.label}. Change scheme`}
        className="flex items-center gap-2 rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500"
      >
        <Swatches colors={active.swatches} />
        <span className="hidden sm:inline">{active.label}</span>
        <svg
          className={`h-4 w-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
        >
          <path
            fillRule="evenodd"
            d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.06l3.71-3.83a.75.75 0 1 1 1.08 1.04l-4.25 4.39a.75.75 0 0 1-1.08 0L5.21 8.27a.75.75 0 0 1 .02-1.06Z"
            clipRule="evenodd"
          />
        </svg>
      </button>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label="Display schemes"
            style={{ position: 'fixed', top: menuPos.top, right: menuPos.right }}
            className="z-[100] w-72 overflow-hidden rounded-lg border border-slate-200 bg-slate-50 p-1 shadow-lg"
          >
            <p className="px-3 pt-2 pb-1 text-xs font-semibold tracking-wide text-slate-400 uppercase">
              Display scheme
            </p>
            {THEMES.map((t) => {
              const isActive = t.id === theme
              return (
                <button
                  key={t.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={isActive}
                  onClick={() => {
                    setTheme(t.id)
                    setOpen(false)
                  }}
                  className={`flex w-full items-center gap-3 rounded-md px-3 py-2 text-left transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 ${
                    isActive
                      ? 'bg-sky-50 text-slate-900'
                      : 'text-slate-700 hover:bg-slate-100'
                  }`}
                >
                  <Swatches colors={t.swatches} />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium">{t.label}</span>
                    <span className="block truncate text-xs text-slate-500">
                      {t.description}
                    </span>
                  </span>
                  {isActive && (
                    <svg
                      className="h-4 w-4 shrink-0 text-sky-600"
                      viewBox="0 0 20 20"
                      fill="currentColor"
                      aria-hidden="true"
                    >
                      <path
                        fillRule="evenodd"
                        d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.5a1 1 0 0 1-1.4 0l-3.5-3.5a1 1 0 1 1 1.4-1.4l2.8 2.79 6.8-6.79a1 1 0 0 1 1.4 0Z"
                        clipRule="evenodd"
                      />
                    </svg>
                  )}
                </button>
              )
            })}
          </div>,
          document.body,
        )}
    </div>
  )
}

export default ThemeSwitcher
