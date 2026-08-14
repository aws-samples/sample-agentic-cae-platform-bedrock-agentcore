import type {
  ModelSource,
  PartMetadata,
  SpecDimension,
  SpecGdtCallout,
  SpecNote,
} from './types'

/** Narrow an unknown value to a non-null object for safe key access. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function asString(value: unknown): string | undefined {
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return undefined
}

function asStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const out = value.map(asString).filter((s): s is string => s !== undefined)
  return out.length > 0 ? out : undefined
}

/**
 * Defensively parse a {@link PartMetadata} from a raw metadata object,
 * tolerating both camelCase and the extraction pipeline's snake_case / nested
 * `title_block` shape. Returns `undefined` when nothing usable is present.
 */
export function parseMetadata(raw: unknown): PartMetadata | undefined {
  if (!isRecord(raw)) return undefined
  const tb = isRecord(raw.title_block) ? raw.title_block : {}

  const dimensions: SpecDimension[] = Array.isArray(raw.dimensions)
    ? raw.dimensions
        .map((d): SpecDimension | null => {
          if (!isRecord(d)) return null
          const feature = asString(d.feature) ?? asString(d.name)
          const value = asString(d.value)
          if (!feature || value === undefined) return null
          return {
            feature,
            value,
            type: asString(d.type),
            tolerance: asString(d.tolerance),
          }
        })
        .filter((d): d is SpecDimension => d !== null)
    : []

  const gdtCallouts: SpecGdtCallout[] = Array.isArray(raw.gdt_callouts)
    ? raw.gdt_callouts
        .map((g): SpecGdtCallout | null => {
          if (!isRecord(g)) return null
          const characteristic = asString(g.characteristic)
          if (!characteristic) return null
          return {
            characteristic,
            toleranceValue: asString(g.tolerance_value) ?? asString(g.toleranceValue),
            zoneShape: asString(g.zone_shape) ?? asString(g.zoneShape),
            modifier: asString(g.modifier),
            datumRefs: asStringArray(g.datum_refs) ?? asStringArray(g.datumRefs),
            appliedTo: asString(g.applied_to) ?? asString(g.appliedTo),
          }
        })
        .filter((g): g is SpecGdtCallout => g !== null)
    : []

  const notes: SpecNote[] = Array.isArray(raw.notes)
    ? raw.notes
        .map((n): SpecNote | null => {
          if (typeof n === 'string') return { text: n }
          if (!isRecord(n)) return null
          const text = asString(n.text)
          return text ? { text, type: asString(n.type) } : null
        })
        .filter((n): n is SpecNote => n !== null)
    : []

  const metadata: PartMetadata = {
    drawingId: asString(raw.drawing_id) ?? asString(raw.drawingId) ?? asString(tb.drawing_number),
    title: asString(raw.title) ?? asString(tb.title),
    material: asString(raw.material) ?? asString(tb.material),
    revision: asString(raw.revision) ?? asString(tb.revision),
    weight: asString(raw.weight) ?? asString(tb.weight),
    dimensions,
    gdtCallouts,
    notes,
  }

  const hasAny =
    metadata.drawingId ||
    metadata.title ||
    metadata.material ||
    metadata.revision ||
    metadata.weight ||
    dimensions.length > 0 ||
    gdtCallouts.length > 0 ||
    notes.length > 0
  return hasAny ? metadata : undefined
}

/**
 * Defensively derive STL {@link ModelSource}s from a WebSocket `result` payload
 * (the `generate_3d_model` output). The backend shape is not yet frozen, so we
 * accept several plausible spellings without throwing on unexpected input:
 *
 *  - `{ model_url | stl_url | url: string }`            → single model
 *  - `{ models: [{ id?, stl_url|url|model_url, label? }] }` → many models
 *  - `{ model_urls: string[] }`                          → many models
 *
 * Returns `[]` for anything unrecognised so the panel falls back to its empty
 * state rather than crashing (Req 6.5, defensive WS read).
 */
export function deriveModelsFromPayload(payload: unknown): ModelSource[] {
  if (!isRecord(payload)) return []

  // Array of model descriptors.
  if (Array.isArray(payload.models)) {
    return payload.models
      .map((entry, index): ModelSource | null => {
        if (!isRecord(entry)) {
          const url = asString(entry)
          return url ? { id: `model-${index}`, stlUrl: url } : null
        }
        const url =
          asString(entry.stl_url) ?? asString(entry.url) ?? asString(entry.model_url)
        if (!url) return null
        return {
          id: asString(entry.id) ?? `model-${index}`,
          stlUrl: url,
          label: asString(entry.label),
          metadata: parseMetadata(entry.metadata),
        }
      })
      .filter((m): m is ModelSource => m !== null)
  }

  // Array of bare URLs.
  if (Array.isArray(payload.model_urls)) {
    return payload.model_urls
      .map((u, index): ModelSource | null => {
        const url = asString(u)
        return url ? { id: `model-${index}`, stlUrl: url } : null
      })
      .filter((m): m is ModelSource => m !== null)
  }

  // Single URL under any of the accepted keys.
  const single =
    asString(payload.model_url) ?? asString(payload.stl_url) ?? asString(payload.url)
  if (single) {
    return [
      {
        id: 'model-0',
        stlUrl: single,
        label: asString(payload.label),
        metadata: parseMetadata(payload.metadata),
      },
    ]
  }

  return []
}
