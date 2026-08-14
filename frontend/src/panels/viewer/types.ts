/**
 * TypeScript model for the 3D Viewer panel (design §6, Requirements 6.2–6.5).
 *
 * Models are STL files loaded from S3 URLs. The STL URL(s) arrive as props/state
 * (ultimately from the `generate_3d_model` output delivered over the WebSocket
 * `result` payload), so the panel renders purely from typed data and works in
 * isolation with sample/empty inputs.
 */

/** A 3D-space position `[x, y, z]` used to anchor an annotation label. */
export type Vec3 = readonly [number, number, number]

/** Category of engineering annotation, driving label styling (Req 6.4). */
export type AnnotationKind = 'dimension' | 'gdt' | 'note'

/**
 * Full detail revealed when an annotation label is expanded (Req 6.4):
 * dimension value, tolerance, GD&T datum references, and/or free-form note text.
 * All fields optional so any annotation kind can populate the relevant subset.
 */
export interface AnnotationDetail {
  /** Nominal value, e.g. `"25.0 mm"` (dimensions). */
  value?: string
  /** Tolerance band, e.g. `"±0.1"` or `"+0.2 / -0.0"`. */
  tolerance?: string
  /** GD&T datum references, e.g. `["A", "B", "C"]`. */
  datumRefs?: readonly string[]
  /** Free-form note / callout text. */
  text?: string
}

/**
 * A single overlayed annotation. Rendered as a clickable CSS2D label anchored at
 * {@link position}; clicking expands {@link detail} (Req 6.4).
 */
export interface Annotation {
  /** Stable unique id (used as the DOM/React key). */
  id: string
  kind: AnnotationKind
  /** Short label shown collapsed, e.g. `"⌀25"`. */
  label: string
  /** World-space anchor point on the model. */
  position: Vec3
  detail: AnnotationDetail
}

/** A single dimension row in the part spec sidebar. */
export interface SpecDimension {
  /** Feature the dimension applies to, e.g. `"Bore"`. */
  feature: string
  /** Nominal value, e.g. `"25.0"`. */
  value: string
  /** Dimension type; `"diameter"` prefixes the value with Ø. */
  type?: string
  /** Tolerance band, e.g. `"±0.05"`. */
  tolerance?: string
}

/** A single GD&T callout in the part spec sidebar. */
export interface SpecGdtCallout {
  /** Geometric characteristic, e.g. `"Position"`, `"Flatness"`. */
  characteristic: string
  /** Tolerance value in mm. */
  toleranceValue?: string
  /** `"diameter"` renders the ⌀ zone modifier. */
  zoneShape?: string
  /** Material-condition modifier, e.g. `"MMC"`. */
  modifier?: string
  /** Datum references, e.g. `["A", "B"]`. */
  datumRefs?: readonly string[]
  /** Feature the callout is applied to. */
  appliedTo?: string
}

/** A free-form note/annotation in the part spec sidebar. */
export interface SpecNote {
  /** Note category, e.g. `"finish"`, `"general"`. */
  type?: string
  text: string
}

/**
 * Engineering metadata for a part, shown in the viewer's spec sidebar
 * (parity with the original 3d-drawing module's info panel): title-block
 * fields plus dimension, GD&T, and note tables.
 */
export interface PartMetadata {
  drawingId?: string
  title?: string
  material?: string
  revision?: string
  weight?: string
  dimensions?: readonly SpecDimension[]
  gdtCallouts?: readonly SpecGdtCallout[]
  notes?: readonly SpecNote[]
}

/**
 * One model to display in the viewer. `stlUrl` is a (possibly presigned) S3 URL.
 * `annotations` overlay onto this model; `label` names it in comparison mode.
 */
export interface ModelSource {
  /** Stable unique id (used as the React key in comparison mode). */
  id: string
  /** Presigned S3 URL to the STL mesh. */
  stlUrl: string
  /** Human-readable name shown above the canvas (e.g. `"Design 3"`). */
  label?: string
  /** Engineering annotations overlaid on this model. */
  annotations?: readonly Annotation[]
  /** Engineering spec metadata shown in the sidebar. */
  metadata?: PartMetadata
}

/**
 * Imperative controls a {@link ModelSource}'s canvas exposes to the toolbar
 * (camera presets, reset, and screenshot), mirroring the original viewer's
 * toolbar. Registered with the panel on mount so one toolbar can drive one or
 * more canvases.
 */
export interface ModelViewHandle {
  /** Frame the model in a default 3/4 view (Req 6.2). */
  resetView(): void
  /** Snap the camera to an orthographic-style Front/Top/Right preset. */
  setView(view: 'front' | 'top' | 'right'): void
  /** Render and download the current canvas as a PNG. */
  screenshot(filename: string): void
}

/** Optional 2D drawing shown beside the 3D model in the dual-panel view (Req 6.3). */
export interface DrawingSource {
  /** Presigned S3 URL to the drawing (SVG or PDF). */
  url: string
  /** MIME-ish hint controlling the render element; defaults to inference by extension. */
  format?: 'svg' | 'pdf' | 'image'
  label?: string
}

/** Maximum number of models displayed side-by-side (Req 6.5). */
export const MAX_MODELS = 4
