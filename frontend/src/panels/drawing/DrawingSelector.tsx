import { useState } from 'react'
import type { UploadedDrawing } from './types'

interface DrawingSelectorProps {
  drawings: UploadedDrawing[]
  selectedId: string | null
  onSelect: (id: string) => void
  onDelete?: (id: string) => void
}

/**
 * Compact dropdown selector for uploaded drawings with a delete button.
 * Sits in the toolbar strip above the chat — shows all the user's drawings
 * (global, not session-scoped) with a trash icon to remove them permanently.
 */
function DrawingSelector({ drawings, selectedId, onSelect, onDelete }: DrawingSelectorProps) {
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  if (drawings.length === 0) {
    return (
      <span className="text-xs text-slate-400 italic">No drawings uploaded</span>
    )
  }

  const selectedDrawing = drawings.find((d) => d.drawingId === selectedId)

  return (
    <div className="flex items-center gap-2">
      <svg
        aria-hidden="true"
        viewBox="0 0 20 20"
        fill="currentColor"
        className="h-4 w-4 text-slate-400"
      >
        <path
          fillRule="evenodd"
          d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z"
          clipRule="evenodd"
        />
      </svg>
      <select
        value={selectedId ?? ''}
        onChange={(e) => onSelect(e.target.value)}
        className="max-w-[200px] rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-700 shadow-sm focus-visible:border-sky-500 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-sky-500"
        aria-label="Select drawing"
      >
        {drawings.map((d) => (
          <option key={d.drawingId} value={d.drawingId}>
            {d.filename}
          </option>
        ))}
      </select>
      <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-semibold text-sky-700">
        {drawings.length}
      </span>

      {/* Delete button for the currently selected drawing */}
      {onDelete && selectedDrawing && (
        <>
          {confirmDelete === selectedId ? (
            <div className="flex items-center gap-1">
              <span className="text-[10px] text-red-600">Delete?</span>
              <button
                type="button"
                onClick={() => {
                  onDelete(selectedDrawing.drawingId)
                  setConfirmDelete(null)
                }}
                className="rounded px-1.5 py-0.5 text-[10px] font-bold text-red-600 hover:bg-red-100"
              >
                Yes
              </button>
              <button
                type="button"
                onClick={() => setConfirmDelete(null)}
                className="rounded px-1.5 py-0.5 text-[10px] font-bold text-slate-500 hover:bg-slate-100"
              >
                No
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDelete(selectedId)}
              className="rounded p-1 text-slate-400 transition-colors hover:bg-red-50 hover:text-red-500"
              title="Delete this drawing"
              aria-label={`Delete ${selectedDrawing.filename}`}
            >
              <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clipRule="evenodd" />
              </svg>
            </button>
          )}
        </>
      )}
    </div>
  )
}

export default DrawingSelector
