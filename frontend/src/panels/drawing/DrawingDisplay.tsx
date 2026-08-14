import type { UploadedDrawing } from './types'

interface DrawingDisplayProps {
  /** The drawing to render, or null when nothing is selected. */
  drawing: UploadedDrawing | null
}

/** True when the drawing should be rendered as an SVG image rather than a PDF. */
function isSvg(drawing: UploadedDrawing): boolean {
  return (
    drawing.contentType.includes('svg') || drawing.filename.toLowerCase().endsWith('.svg')
  )
}

/**
 * Renders the selected drawing using browser-native viewers only — no heavy PDF
 * libraries (Req 8.1). SVGs render via `<img>`; PDFs render via `<object>` with
 * an in-browser fallback link for environments without an embedded PDF viewer.
 */
function DrawingDisplay({ drawing }: DrawingDisplayProps) {
  if (drawing === null) {
    return (
      <div className="flex h-full min-h-64 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 p-6 text-center text-sm text-slate-500">
        Upload or select a drawing to view it here.
      </div>
    )
  }

  if (isSvg(drawing)) {
    return (
      <div className="h-full min-h-64 overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-4">
        <img
          src={drawing.objectUrl}
          alt={`Drawing ${drawing.filename}`}
          className="mx-auto max-w-full"
        />
      </div>
    )
  }

  return (
    <object
      data={drawing.objectUrl}
      type="application/pdf"
      aria-label={`Drawing ${drawing.filename}`}
      className="h-full min-h-96 w-full rounded-lg border border-slate-200 bg-slate-50"
    >
      <div className="flex h-full items-center justify-center p-6 text-center text-sm text-slate-600">
        <p>
          Unable to display the PDF inline.{' '}
          <a
            href={drawing.objectUrl}
            target="_blank"
            rel="noreferrer"
            className="font-medium text-sky-600 underline hover:text-sky-700"
          >
            Open {drawing.filename} in a new tab
          </a>
          .
        </p>
      </div>
    </object>
  )
}

export default DrawingDisplay
