import { useCallback, useEffect, useRef, useState } from 'react'
import { useSession } from '../session/useSession'
import SessionSidebar from '../session/SessionSidebar'
import ChatPanel from './ChatPanel'
import DrawingDisplay from './drawing/DrawingDisplay'
import ExtractionBar from './drawing/ExtractionBar'
import { useDrawings } from './drawing/DrawingContext'
import {
  confirmUpload,
  putFile,
  requestPresignedUrl,
  validateUploadFile,
} from '../api/upload'

type UploadPhase = 'idle' | 'uploading' | 'confirming' | 'done' | 'error'

/**
 * Redesigned unified workspace — a professional, chat-centric single-screen
 * experience inspired by modern AI assistant UIs:
 *
 * ┌─────────────────────────────────────────────────────────────────┐
 * │ [Conversations ▸] │ [📄 drawing.pdf ▾ (2)]  │ [Extraction ●●●○] │
 * ├────────┬──────────────────────────────────────┬─────────────────┤
 * │        │                                      │                 │
 * │Sessions│          Chat messages               │  Drawing viewer │
 * │        │                                      │                 │
 * │        ├──────────────────────────────────────┤                 │
 * │        │ [📎 Upload] [message...] [⚡Opt][Send]│                 │
 * └────────┴──────────────────────────────────────┴─────────────────┘
 *
 * Key changes from the previous layout:
 *   - Upload button is an icon/clip button at the bottom-left of the chat input
 *   - Drawings are a compact dropdown in the toolbar at the top
 *   - Extraction progress is a simplified horizontal pipeline bar
 *   - Session sidebar is collapsible (unchanged from before)
 *   - The drawing viewer sits as a right panel (shown when a drawing is selected)
 */
function WorkspacePanel() {
  const { activeSessionId, selectSession } = useSession()
  const [collapsed, setCollapsed] = useState(false)
  const [phase, setPhase] = useState<UploadPhase>('idle')
  const [uploadStarted, setUploadStarted] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const objectUrls = useRef<string[]>([])
  const sessionKey = activeSessionId ?? 'none'

  useEffect(
    () => () => {
      for (const url of objectUrls.current) URL.revokeObjectURL(url)
    },
    [],
  )

  // Drawing list + selection now live in the shared DrawingContext so the
  // selector can sit in the app shell's second-level toolbar (visible across
  // all tabs). The Workspace reads the same context for its viewer + chat
  // scoping, and pushes uploads into it.
  const drawingCtx = useDrawings()
  const selectedId = drawingCtx?.selectedId ?? null
  const selected = drawingCtx?.selected ?? null

  const handleUpload = useCallback(
    async (file: File) => {
      setUploadError(null)
      const errors = validateUploadFile(file)
      if (errors.length > 0) {
        setUploadError(errors.join(' '))
        return
      }
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
        drawingCtx?.addLocalDrawing({
          drawingId,
          filename: file.name,
          contentType: file.type,
          objectUrl,
        })
        setPhase('done')
        if (activeSessionId) void selectSession(activeSessionId)
      } catch (err) {
        setPhase('error')
        setUploadError(err instanceof Error ? err.message : 'Upload failed.')
      }
    },
    [activeSessionId, selectSession, drawingCtx],
  )

  const isBusy = phase === 'uploading' || phase === 'confirming'

  const columns = collapsed
    ? 'lg:grid-cols-[auto_minmax(0,1fr)_minmax(0,1fr)]'
    : 'lg:grid-cols-[200px_minmax(0,1fr)_minmax(0,1fr)]'

  const showViewer = selected !== null && selected.objectUrl

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      {/* Toolbar strip: extraction progress (the drawing selector now lives in
          the app shell's second-level toolbar, alongside the workflow tabs). */}
      <div className="flex flex-wrap items-center gap-3">
        <ExtractionBar active={uploadStarted} phase={phase} />
        {uploadError && (
          <span className="text-xs text-red-600">{uploadError}</span>
        )}
      </div>

      {/* Main content grid */}
      <div className={`grid min-h-0 flex-1 grid-cols-1 gap-3 ${showViewer ? columns : collapsed ? 'lg:grid-cols-[auto_minmax(0,1fr)]' : 'lg:grid-cols-[200px_minmax(0,1fr)]'}`}>
        {/* Session sidebar */}
        {collapsed ? (
          <div className="hidden lg:flex lg:h-full lg:flex-col">
            <button
              type="button"
              onClick={() => setCollapsed(false)}
              aria-label="Expand conversations"
              title="Conversations"
              className="flex flex-col items-center gap-2 rounded-lg border border-slate-200 bg-white px-2 py-3 text-slate-500 shadow-sm transition-colors hover:bg-slate-50 hover:text-slate-900"
            >
              <svg aria-hidden="true" viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M8 5l5 5-5 5" />
              </svg>
              <span className="text-[10px] font-medium [writing-mode:vertical-rl]">Chats</span>
            </button>
          </div>
        ) : (
          <div className="hidden min-h-0 lg:block lg:h-full">
            <SessionSidebar onCollapse={() => setCollapsed(true)} />
          </div>
        )}

        {/* Chat panel with integrated upload button */}
        <div className="relative min-h-0 lg:h-full">
          <ChatPanel
            key={sessionKey}
            activeDrawingId={selectedId ?? undefined}
            uploadSlot={
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={isBusy}
                title="Upload drawing (PDF)"
                className="flex h-10 w-10 items-center justify-center rounded-lg border border-slate-200 text-slate-500 transition-colors hover:bg-slate-100 hover:text-sky-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isBusy ? (
                  <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="10" strokeOpacity="0.25" />
                    <path d="M12 2a10 10 0 019.9 8.5" strokeLinecap="round" />
                  </svg>
                ) : (
                  <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M15 7l-5-5-5 5M10 2v12M3 14v2a2 2 0 002 2h10a2 2 0 002-2v-2" />
                  </svg>
                )}
              </button>
            }
          />
          <input
            ref={fileInputRef}
            type="file"
            accept="application/pdf,.pdf"
            className="sr-only"
            disabled={isBusy}
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) void handleUpload(file)
              e.target.value = ''
            }}
          />
        </div>

        {/* Drawing viewer (right panel, only when a drawing is selected) */}
        {showViewer && (
          <div className="hidden min-h-0 overflow-y-auto lg:block">
            <DrawingDisplay drawing={selected} />
          </div>
        )}
      </div>
    </div>
  )
}

export default WorkspacePanel
