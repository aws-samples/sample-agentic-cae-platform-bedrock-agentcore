import type { DrawingSource } from './types'

interface DrawingPaneProps {
  drawing: DrawingSource
}

/** Infer the render element from an explicit format hint or the URL extension. */
function inferFormat(drawing: DrawingSource): 'pdf' | 'svg' | 'image' {
  if (drawing.format) return drawing.format
  const lower = drawing.url.split('?')[0].toLowerCase()
  if (lower.endsWith('.pdf')) return 'pdf'
  if (lower.endsWith('.svg')) return 'svg'
  return 'image'
}

/**
 * Renders the corresponding 2D engineering drawing (SVG/PDF/image) beside the
 * 3D model in the dual-panel layout (Req 6.3). PDFs use a native `<object>`
 * (browser viewer); SVG/raster use `<img>`. No third-party PDF dependency.
 */
function DrawingPane({ drawing }: DrawingPaneProps) {
  const format = inferFormat(drawing)

  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-md border border-slate-200 bg-slate-50">
      <div className="border-b border-slate-200 px-3 py-1.5 text-xs font-semibold text-slate-600">
        {drawing.label ?? '2D Drawing'}
      </div>
      <div className="min-h-0 flex-1 bg-slate-50">
        {format === 'pdf' ? (
          <object
            data={drawing.url}
            type="application/pdf"
            className="h-full w-full"
            aria-label={drawing.label ?? '2D drawing (PDF)'}
          >
            <a href={drawing.url} className="text-sky-600 underline">
              Open drawing
            </a>
          </object>
        ) : (
          <img
            src={drawing.url}
            alt={drawing.label ?? '2D engineering drawing'}
            className="h-full w-full object-contain"
          />
        )}
      </div>
    </div>
  )
}

export default DrawingPane
