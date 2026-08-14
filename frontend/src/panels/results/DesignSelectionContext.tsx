/**
 * Shared context for design selection state between the Results Dashboard
 * and the Viewer Panel (Req 4.3). Allows the Results Dashboard to broadcast
 * which designs are selected and their model generation states, so the Viewer
 * Panel can display the corresponding models in a side-by-side comparison grid.
 */
import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type { ModelGenerationState } from './types'

export interface DesignSelectionContextValue {
  /** Currently selected design IDs from the Results Dashboard (up to 4). */
  selectedIds: string[]
  /** Model generation states for all designs that have been generated. */
  modelStates: ModelGenerationState[]
  /**
   * Retry handler for a failed design's 3D generation, registered by the
   * Results Dashboard so the Viewer Panel (a sibling route) can re-trigger
   * generation via the shared channel (Req 5.3). `null` until registered.
   */
  onRetryGeneration: ((designId: string) => void) | null
  /** Update the selected design IDs (called by Results Dashboard). */
  setSelectedIds: (ids: string[]) => void
  /** Update model generation states (called by Results Dashboard). */
  setModelStates: (states: ModelGenerationState[]) => void
  /** Register the retry handler (called by Results Dashboard). */
  setOnRetryGeneration: (handler: ((designId: string) => void) | null) => void
}

export const DesignSelectionContext = createContext<DesignSelectionContextValue | null>(null)

/** Provider that wraps the app layout to share design selection across panels. */
export function DesignSelectionProvider({ children }: { children: ReactNode }) {
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [modelStates, setModelStates] = useState<ModelGenerationState[]>([])
  const [onRetryGeneration, setOnRetryGenerationState] = useState<
    ((designId: string) => void) | null
  >(null)

  // Wrap in a state updater so React stores the function itself instead of
  // treating a passed handler as a functional state update.
  const setOnRetryGeneration = useCallback(
    (handler: ((designId: string) => void) | null) =>
      setOnRetryGenerationState(() => handler),
    [],
  )

  const value = useMemo<DesignSelectionContextValue>(
    () => ({
      selectedIds,
      modelStates,
      onRetryGeneration,
      setSelectedIds,
      setModelStates,
      setOnRetryGeneration,
    }),
    [selectedIds, modelStates, onRetryGeneration, setOnRetryGeneration],
  )

  return (
    <DesignSelectionContext.Provider value={value}>
      {children}
    </DesignSelectionContext.Provider>
  )
}

/** Hook to consume the design selection context; returns null if not mounted. */
export function useDesignSelection(): DesignSelectionContextValue | null {
  return useContext(DesignSelectionContext)
}
