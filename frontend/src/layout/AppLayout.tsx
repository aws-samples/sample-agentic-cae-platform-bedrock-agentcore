import { NavLink, Outlet } from 'react-router-dom'
import { NAV_ITEMS } from './navItems'
import ConnectionStatus from '../ws/ConnectionStatus'
import ProfileMenu from './ProfileMenu'
import DrawingSelector from '../panels/drawing/DrawingSelector'
import { useDrawings } from '../panels/drawing/DrawingContext'
import { useSession } from '../session/useSession'
import { deleteDrawing } from '../api/upload'

/**
 * App shell for the Agentic CAE Platform.
 *
 * Two-tier navigation:
 *   Level 1 (app bar): branding + connection status + profile menu.
 *   Level 2 (sub-nav): the CAE workflow tabs (Pre-Processing / Design
 *     Exploration / Post-Processing) on the left, and the active-drawing
 *     selector on the right. The drawing is cross-cutting context shared by
 *     all three stages, so it sits at the same level as the tabs and stays
 *     visible on every tab.
 */
function AppLayout() {
  const baseTabClasses =
    'rounded-md px-3 py-1.5 text-sm font-medium whitespace-nowrap transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500'

  const drawingCtx = useDrawings()
  const { activeSessionId, selectSession } = useSession()

  async function handleDeleteDrawing(drawingId: string): Promise<void> {
    try {
      await deleteDrawing(drawingId)
      drawingCtx?.removeDrawing(drawingId)
      // Refresh the active session so persisted drawings reflect the deletion.
      if (activeSessionId) void selectSession(activeSessionId)
    } catch {
      /* best-effort; the selector stays on its current value on failure */
    }
  }

  return (
    <div className="flex h-full min-h-screen w-full flex-col bg-slate-100 text-slate-900">
      {/* Level 1 — app bar: branding + status + profile */}
      <header className="border-b border-slate-200 bg-white/80 backdrop-blur-sm">
        <div className="mx-auto flex w-full max-w-[2560px] items-center justify-between px-4 py-2 sm:px-6">
          <span className="text-base font-bold tracking-tight text-slate-900">
            🏎️ <span className="hidden sm:inline">Crew</span>Chief<span className="text-sky-600">AI</span>
          </span>
          <div className="flex items-center gap-3">
            <ConnectionStatus />
            <ProfileMenu />
          </div>
        </div>
      </header>

      {/* Level 2 — sub-nav: workflow tabs (left) + active drawing (right) */}
      <div className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex w-full max-w-[2560px] flex-wrap items-center justify-between gap-2 px-4 py-1.5 sm:px-6">
          <nav aria-label="Workflow stages">
            <ul className="flex gap-1">
              {NAV_ITEMS.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    className={({ isActive }) =>
                      isActive
                        ? `${baseTabClasses} bg-sky-600 text-white`
                        : `${baseTabClasses} text-slate-500 hover:bg-slate-100 hover:text-slate-900`
                    }
                  >
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>

          {/* Active-drawing selector — shared context across all tabs */}
          <div className="flex items-center gap-2">
            <span className="hidden text-xs font-medium uppercase tracking-wide text-slate-400 sm:inline">
              Active drawing
            </span>
            <DrawingSelector
              drawings={drawingCtx?.drawings ?? []}
              selectedId={drawingCtx?.selectedId ?? null}
              onSelect={(id) => drawingCtx?.setSelectedId(id)}
              onDelete={handleDeleteDrawing}
            />
          </div>
        </div>
      </div>

      <main className="flex-1 overflow-x-hidden">
        <div className="mx-auto h-full w-full max-w-[2560px] px-4 py-3 sm:px-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

export default AppLayout
