import type { PartMetadata } from './types'

interface SpecSidebarProps {
  /** Part metadata to display; when absent the sidebar shows a hint. */
  metadata?: PartMetadata
}

function InfoRow({ label, value }: { label: string; value?: string }) {
  if (!value) return null
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
      </span>
      <span className="text-right text-sm font-medium text-slate-800">{value}</span>
    </div>
  )
}

function SectionHeading({ children }: { children: string }) {
  return (
    <h3 className="mt-4 mb-1 text-xs font-semibold uppercase tracking-wide text-sky-600">
      {children}
    </h3>
  )
}

/**
 * Part specification sidebar for the 3D Viewer — parity with the original
 * 3d-drawing module's info panel. Shows title-block fields plus dimension,
 * GD&T, and note tables for the selected model's {@link PartMetadata}.
 */
function SpecSidebar({ metadata }: SpecSidebarProps) {
  if (!metadata) {
    return (
      <aside className="flex h-full w-full flex-col overflow-y-auto rounded-md border border-slate-200 bg-slate-50 p-4">
        <h2 className="text-sm font-semibold text-slate-900">Part Information</h2>
        <p className="mt-2 text-xs text-slate-500">
          Specification details (material, dimensions, GD&amp;T, and notes) appear
          here once a model with extracted metadata is loaded.
        </p>
      </aside>
    )
  }

  const dimensions = metadata.dimensions ?? []
  const gdt = metadata.gdtCallouts ?? []
  const notes = metadata.notes ?? []

  return (
    <aside className="flex h-full w-full flex-col overflow-y-auto rounded-md border border-slate-200 bg-slate-50 p-4">
      <h2 className="text-sm font-semibold text-slate-900">Part Information</h2>
      <div className="mt-1 divide-y divide-slate-200">
        <InfoRow label="Drawing" value={metadata.drawingId} />
        <InfoRow label="Title" value={metadata.title} />
        <InfoRow label="Material" value={metadata.material} />
        <InfoRow label="Revision" value={metadata.revision} />
        <InfoRow label="Weight" value={metadata.weight} />
      </div>

      {dimensions.length > 0 && (
        <>
          <SectionHeading>Dimensions</SectionHeading>
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-slate-500">
                <th className="py-1 pr-2 font-medium">Feature</th>
                <th className="py-1 pr-2 font-medium">Value (mm)</th>
                <th className="py-1 font-medium">Tol.</th>
              </tr>
            </thead>
            <tbody className="text-slate-700">
              {dimensions.map((d, i) => (
                <tr key={`${d.feature}-${i}`} className="border-t border-slate-200">
                  <td className="py-1 pr-2">{d.feature}</td>
                  <td className="py-1 pr-2 tabular-nums">
                    {d.type === 'diameter' ? 'Ø' : ''}
                    {d.value}
                  </td>
                  <td className="py-1 tabular-nums">{d.tolerance ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {gdt.length > 0 && (
        <>
          <SectionHeading>GD&amp;T Callouts</SectionHeading>
          <ul className="flex flex-col gap-2">
            {gdt.map((g, i) => (
              <li
                key={`${g.characteristic}-${i}`}
                className="rounded border border-slate-200 bg-slate-100 p-2 text-xs text-slate-700"
              >
                <span className="font-semibold text-slate-900">
                  {g.characteristic}
                </span>{' '}
                {g.toleranceValue ?? ''}
                {g.zoneShape === 'diameter' ? ' ⌀' : ''}
                {g.modifier ? ` (${g.modifier})` : ''}
                {g.datumRefs && g.datumRefs.length > 0
                  ? ` | ${g.datumRefs.join(', ')}`
                  : ''}
                {g.appliedTo && (
                  <span className="mt-0.5 block text-[11px] text-slate-500">
                    {g.appliedTo}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {notes.length > 0 && (
        <>
          <SectionHeading>Notes</SectionHeading>
          <ul className="flex flex-col gap-1.5 text-xs text-slate-700">
            {notes.map((n, i) => (
              <li key={i}>
                {n.type && (
                  <span className="mr-1 text-[11px] font-medium uppercase text-slate-500">
                    [{n.type}]
                  </span>
                )}
                {n.text}
              </li>
            ))}
          </ul>
        </>
      )}
    </aside>
  )
}

export default SpecSidebar
