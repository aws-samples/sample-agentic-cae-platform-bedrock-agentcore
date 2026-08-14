import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  confirmUpload,
  putFile,
  requestPresignedUrl,
  validateUploadFile,
} from '../api/upload'
import { useSession } from '../session/useSession'
import DrawingDisplay from './drawing/DrawingDisplay'
import ExtractionProgress from './drawing/ExtractionProgress'
import FileDropzone from './drawing/FileDropzone'
import type { UploadedDrawing } from './drawing/types'

type UploadPhase = 'idle' | 'uploading' | 'confirming' | 'done' | 'error'

/**
 * Drawing Viewer panel: drag-and-drop PDF upload via the presigned-URL flow,
 * live extraction progress, and a browser-native PDF/SVG viewer (Req 1.1, 8.2).
 *
 * Upload uses the HTTP API (Req 8.3): request a presigned URL, PUT the bytes to
 * S3, then confirm. Extraction progress and the auto-transition to Chat are
 * handled by {@link ExtractionProgress} from WebSocket `stage` frames.
 */
function DrawingViewerPanel() {
  const { activeSession, activeSessionId, selectSession } = useSession()
  // Drawings uploaded in this browser session (instant local preview via blob URL).
  const [localDrawings, setLocalDrawings] = useState<UploadedDrawing[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [phase, setPhase] = useState<UploadPhase>('idle')
  const [validationErrors, setValidationErrors] = useState<string[]>([])
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [uploadStarted, setUploadStarted] = useState(false)

  // Drawings persisted server-side, restored from the active session (Req 13.3).
  const persistedDrawings = useMemo<UploadedDrawing[]>(() => {
    const list = activeSession?.drawings ?? []
    return list.map((d) => {
      const record = d as Record<string, unknown>
      const viewUrl = typeof record.viewUrl === 'string' ? record.viewUrl : ''
      const filename =
        typeof record.filename === 'string' && record.filename
          ? record.filename
          : String(d.drawingId)
      return {
        drawingId: String(d.drawingId),
        filename,
        contentType: 'application/pdf',
        objectUrl: viewUrl,
      }
    })
  }, [activeSession])

  // Merge local (freshly uploaded) + persisted drawings, de-duped by id with
  // local taking precedence so a just-uploaded file previews instantly.
  const drawings = useMemo<UploadedDrawing[]>(() => {
    const byId = new Map<string, UploadedDrawing>()
    for (const d of [...localDrawings, ...persistedDrawings]) {
      if (!byId.has(d.drawingId)) {
        byId.set(d.drawingId, d)
      }
    }
    return [...byId.values()]
  }, [localDrawings, persistedDrawings])

  // Track object URLs so they can be revoked on unmount (avoid memory leaks).
  const objectUrls = useRef<string[]>([])
  useEffect(
    () => () => {
      for (const url of objectUrls.current) {
        URL.revokeObjectURL(url)
      }
    },
    [],
  )

  const handleFiles = useCallback(async (files: File[]) => {
    const file = files[0]
    if (!file) {
      return
    }

    setErrorMessage(null)
    const errors = validateUploadFile(file)
    if (errors.length > 0) {
      setValidationErrors(errors)
      return
    }
    setValidationErrors([])
    setUploadStarted(true)

    try {
      setPhase('uploading')
      const { drawingId, uploadUrl } = await requestPresignedUrl({
        filename: file.name,
        contentType: file.type,
        size: file.size,
      })
      await putFile(uploadUrl, file)

      setPhase('confirming')
      await confirmUpload(drawingId, file.name)

      const objectUrl = URL.createObjectURL(file)
      objectUrls.current.push(objectUrl)
      const drawing: UploadedDrawing = {
        drawingId,
        filename: file.name,
        contentType: file.type,
        objectUrl,
      }
      setLocalDrawings((prev) => [drawing, ...prev])
      setSelectedId(drawingId)
      setPhase('done')
      // Refresh the session so the drawing is restored (with a view URL) on
      // reload and appears for other views. Best-effort — local preview already
      // works regardless.
      if (activeSessionId !== null) {
        void selectSession(activeSessionId)
      }
    } catch (error) {
      setPhase('error')
      setErrorMessage(error instanceof Error ? error.message : 'Upload failed.')
    }
  }, [activeSessionId, selectSession])

  // Default the selection to the first available drawing once any exist.
  useEffect(() => {
    if (selectedId === null && drawings.length > 0) {
      setSelectedId(drawings[0].drawingId)
    }
  }, [drawings, selectedId])

  const selected = drawings.find((d) => d.drawingId === selectedId) ?? null
  const isBusy = phase === 'uploading' || phase === 'confirming'

  return (
    <section
      className="flex h-full w-full flex-col gap-4"
      aria-labelledby="drawing-panel-heading"
    >
      <header>
        <h1 id="drawing-panel-heading" className="text-2xl font-semibold text-slate-900">
          Drawing Viewer
        </h1>
        <p className="text-sm text-slate-600">
          Upload an engineering drawing (PDF) to index it for Q&amp;A and optimization.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        {/* Left column: upload + progress + list */}
        <div className="flex flex-col gap-4">
          <FileDropzone onFiles={handleFiles} disabled={isBusy} />

          {isBusy && (
            <p role="status" className="text-sm font-medium text-sky-700">
              {phase === 'uploading' ? 'Uploading drawing…' : 'Confirming upload…'}
            </p>
          )}

          {validationErrors.length > 0 && (
            <ul role="alert" className="flex flex-col gap-1 rounded-md bg-red-50 p-3 text-sm text-red-700">
              {validationErrors.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          )}

          {errorMessage && (
            <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
              {errorMessage}
            </p>
          )}

          <ExtractionProgress active={uploadStarted} />

          {drawings.length > 0 && (
            <nav aria-label="Uploaded drawings" className="rounded-lg border border-slate-200 bg-slate-50 p-3 shadow-sm">
              <h2 className="mb-2 text-sm font-semibold text-slate-900">Uploaded drawings</h2>
              <ul className="flex flex-col gap-1">
                {drawings.map((drawing) => {
                  const isActive = drawing.drawingId === selectedId
                  return (
                    <li key={drawing.drawingId}>
                      <button
                        type="button"
                        onClick={() => setSelectedId(drawing.drawingId)}
                        aria-current={isActive ? 'true' : undefined}
                        className={`w-full truncate rounded-md px-3 py-2 text-left text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 ${
                          isActive
                            ? 'bg-sky-100 font-medium text-sky-900'
                            : 'text-slate-700 hover:bg-slate-100'
                        }`}
                        title={drawing.filename}
                      >
                        {drawing.filename}
                      </button>
                    </li>
                  )
                })}
              </ul>
            </nav>
          )}
        </div>

        {/* Right column: viewer */}
        <div className="min-h-96">
          <DrawingDisplay drawing={selected} />
        </div>
      </div>
    </section>
  )
}

export default DrawingViewerPanel
