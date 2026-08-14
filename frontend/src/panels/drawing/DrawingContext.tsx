/**
 * Shared drawing-selection context.
 *
 * The active drawing is cross-cutting context: the Pre-Processing, Design
 * Exploration, and Post-Processing tabs all operate on the currently-selected
 * drawing. Hoisting the drawing list + selection into a provider (above the
 * routes) lets the drawing selector live in the app shell's second-level
 * toolbar — persistent and visible on every tab — instead of being trapped
 * inside the Workspace panel.
 *
 * Drawings are USER-GLOBAL (not session-scoped): the list is derived from the
 * active session's detail load (which returns all of the user's drawings) and
 * augmented by local uploads for instant preview.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useSession } from '../../session/useSession'
import type { UploadedDrawing } from './types'

export interface DrawingContextValue {
  /** Merged list of the user's drawings (local uploads + persisted). */
  drawings: UploadedDrawing[]
  /** Currently-selected drawing id, or null when none. */
  selectedId: string | null
  /** The selected drawing record, or null. */
  selected: UploadedDrawing | null
  /** Select a drawing by id. */
  setSelectedId: (id: string | null) => void
  /** Add a freshly-uploaded drawing (kept locally for instant preview). */
  addLocalDrawing: (drawing: UploadedDrawing) => void
  /** Remove a drawing from local + persisted state (does not call the API). */
  removeDrawing: (drawingId: string) => void
}

const DrawingContext = createContext<DrawingContextValue | null>(null)

export function DrawingProvider({ children }: { children: ReactNode }) {
  const { activeSession, activeSessionId, selectSession } = useSession()

  const [localDrawings, setLocalDrawings] = useState<UploadedDrawing[]>([])
  const [globalDrawings, setGlobalDrawings] = useState<UploadedDrawing[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const drawingsLoadedRef = useRef(false)

  // Derive the global drawing list whenever a session detail load returns
  // drawings (drawings are user-global, so this list persists across session
  // switches and is never reset to empty on change).
  useEffect(() => {
    const list = activeSession?.drawings ?? []
    if (list.length > 0) {
      drawingsLoadedRef.current = true
      setGlobalDrawings(
        list.map((d) => {
          const record = d as Record<string, unknown>
          return {
            drawingId: String(d.drawingId),
            filename:
              (typeof record.filename === 'string' && record.filename) || String(d.drawingId),
            contentType: 'application/pdf',
            objectUrl: typeof record.viewUrl === 'string' ? record.viewUrl : '',
          }
        }),
      )
    }
  }, [activeSession])

  // Proactively load drawings on mount: if the initial session has no drawings
  // data yet, load the current session's detail (which returns all drawings).
  useEffect(() => {
    if (drawingsLoadedRef.current || !activeSessionId) return
    if (activeSession?.drawings && activeSession.drawings.length > 0) return
    void selectSession(activeSessionId)
    drawingsLoadedRef.current = true
  }, [activeSessionId, activeSession, selectSession])

  const drawings = useMemo<UploadedDrawing[]>(() => {
    const byId = new Map<string, UploadedDrawing>()
    for (const d of localDrawings) {
      byId.set(d.drawingId, d)
    }
    for (const d of globalDrawings) {
      if (!byId.has(d.drawingId)) byId.set(d.drawingId, d)
    }
    return [...byId.values()]
  }, [localDrawings, globalDrawings])

  // Default the selection to the first drawing once any are available.
  useEffect(() => {
    if (selectedId === null && drawings.length > 0) setSelectedId(drawings[0].drawingId)
  }, [drawings, selectedId])

  const selected = drawings.find((d) => d.drawingId === selectedId) ?? null

  const addLocalDrawing = useCallback((drawing: UploadedDrawing) => {
    setLocalDrawings((prev) => [drawing, ...prev.filter((d) => d.drawingId !== drawing.drawingId)])
    setSelectedId(drawing.drawingId)
  }, [])

  const removeDrawing = useCallback(
    (drawingId: string) => {
      setLocalDrawings((prev) => prev.filter((d) => d.drawingId !== drawingId))
      setGlobalDrawings((prev) => prev.filter((d) => d.drawingId !== drawingId))
      setSelectedId((current) => {
        if (current !== drawingId) return current
        const remaining = drawings.filter((d) => d.drawingId !== drawingId)
        return remaining.length > 0 ? remaining[0].drawingId : null
      })
    },
    [drawings],
  )

  const value = useMemo<DrawingContextValue>(
    () => ({ drawings, selectedId, selected, setSelectedId, addLocalDrawing, removeDrawing }),
    [drawings, selectedId, selected, addLocalDrawing, removeDrawing],
  )

  return <DrawingContext.Provider value={value}>{children}</DrawingContext.Provider>
}

/** Access the shared drawing selection. Returns null outside the provider. */
export function useDrawings(): DrawingContextValue | null {
  return useContext(DrawingContext)
}
