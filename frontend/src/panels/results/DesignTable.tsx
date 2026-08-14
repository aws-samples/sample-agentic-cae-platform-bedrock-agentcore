import type { ModelGenerationState, ParetoDesign } from './types'

interface DesignTableProps {
  designs: readonly ParetoDesign[]
  objectiveNames: readonly string[]
  constraintNames: readonly string[]
  selectedIds: readonly string[]
  maxSelection: number
  onToggle: (id: string) => void
  /** Per-design 3D model generation state (Req 3.1, 3.3, 3.4, 3.5). */
  modelStates?: readonly ModelGenerationState[]
  /** Callback to trigger 3D generation for a design (Req 3.1). */
  onGenerate3D?: (design: ParetoDesign) => void
}

function ConstraintBadge({ passed }: { passed: boolean }) {
  return (
    <span
      className={
        passed
          ? 'inline-flex rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700'
          : 'inline-flex rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700'
      }
    >
      {passed ? 'Pass' : 'Fail'}
    </span>
  )
}

/**
 * Inline loading spinner shown while 3D model generation is in progress.
 */
function GeneratingSpinner() {
  return (
    <span className="inline-flex items-center gap-1 text-xs text-sky-600">
      <svg
        className="h-3.5 w-3.5 animate-spin"
        viewBox="0 0 16 16"
        fill="none"
        aria-hidden="true"
      >
        <circle
          cx="8"
          cy="8"
          r="6"
          stroke="currentColor"
          strokeWidth="2"
          strokeDasharray="28"
          strokeDashoffset="8"
          strokeLinecap="round"
        />
      </svg>
      Generating…
    </span>
  )
}

/**
 * Button/status cell for 3D model generation per design row.
 * Shows idle → generating → ready/error states (Req 3.3, 3.4).
 */
function Generate3DCell({
  design,
  state,
  onGenerate,
}: {
  design: ParetoDesign
  state: ModelGenerationState | undefined
  onGenerate: ((design: ParetoDesign) => void) | undefined
}) {
  if (!onGenerate) return null

  const status = state?.status ?? 'idle'

  if (status === 'generating') {
    return (
      <td className="px-3 py-2">
        <GeneratingSpinner />
      </td>
    )
  }

  if (status === 'ready') {
    return (
      <td className="px-3 py-2">
        <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-600">
          <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path
              fillRule="evenodd"
              d="M12.416 3.376a.75.75 0 0 1 .208 1.04l-5 7.5a.75.75 0 0 1-1.154.114l-3-3a.75.75 0 0 1 1.06-1.06l2.353 2.353 4.493-6.74a.75.75 0 0 1 1.04-.207Z"
              clipRule="evenodd"
            />
          </svg>
          3D Ready
        </span>
      </td>
    )
  }

  if (status === 'error') {
    return (
      <td className="px-3 py-2">
        <span className="inline-flex items-center gap-1.5">
          <span className="text-xs text-rose-600" title={state?.error ?? 'Generation failed'}>
            Error
          </span>
          <button
            type="button"
            onClick={() => onGenerate(design)}
            className="rounded bg-rose-50 px-1.5 py-0.5 text-xs font-medium text-rose-700 hover:bg-rose-100"
            aria-label={`Retry 3D generation for design ${design.id}`}
          >
            Retry
          </button>
        </span>
      </td>
    )
  }

  // idle state: show the Generate 3D button
  return (
    <td className="px-3 py-2">
      <button
        type="button"
        onClick={() => onGenerate(design)}
        className="inline-flex items-center gap-1 rounded bg-sky-50 px-2 py-1 text-xs font-medium text-sky-700 hover:bg-sky-100"
        aria-label={`Generate 3D model for design ${design.id}`}
      >
        Generate 3D
      </button>
    </td>
  )
}

/**
 * Design comparison table (Req 5.4 / 8.1): objective values plus per-constraint
 * pass/fail badges and engineering rationale. A leading checkbox column drives
 * the up-to-4 selection for side-by-side 3D comparison; the cap is enforced by
 * `toggleSelection` in the parent, and this table disables unchecked rows once
 * the cap is reached.
 *
 * Each row also includes a "Generate 3D" action column that triggers model
 * generation for the design (Req 3.1, 3.3, 3.4, 3.5).
 */
function DesignTable({
  designs,
  objectiveNames,
  constraintNames,
  selectedIds,
  maxSelection,
  onToggle,
  modelStates,
  onGenerate3D,
}: DesignTableProps) {
  const atCap = selectedIds.length >= maxSelection

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <th scope="col" className="px-3 py-2 font-medium">
              Compare
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Design
            </th>
            {objectiveNames.map((name) => (
              <th key={name} scope="col" className="px-3 py-2 font-medium">
                {name}
              </th>
            ))}
            {constraintNames.map((name) => (
              <th key={name} scope="col" className="px-3 py-2 font-medium">
                {name}
              </th>
            ))}
            <th scope="col" className="px-3 py-2 font-medium">
              Rationale
            </th>
            {onGenerate3D && (
              <th scope="col" className="px-3 py-2 font-medium">
                3D Model
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {designs.map((design) => {
            const selected = selectedIds.includes(design.id)
            const constraintByName = new Map(
              design.constraints.map((constraint) => [constraint.name, constraint]),
            )
            const modelState = modelStates?.find((s) => s.designId === design.id)
            return (
              <tr
                key={design.id}
                className={
                  selected
                    ? 'border-b border-slate-100 bg-blue-50'
                    : 'border-b border-slate-100'
                }
              >
                <td className="px-3 py-2">
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={!selected && atCap}
                    onChange={() => onToggle(design.id)}
                    aria-label={`Select design ${design.id} for comparison`}
                    className="h-4 w-4 accent-blue-600 disabled:opacity-40"
                  />
                </td>
                <td className="px-3 py-2 font-medium text-slate-900">{design.id}</td>
                {objectiveNames.map((name) => (
                  <td key={name} className="px-3 py-2 tabular-nums text-slate-700">
                    {typeof design.objectives[name] === 'number'
                      ? design.objectives[name]
                      : '—'}
                  </td>
                ))}
                {constraintNames.map((name) => {
                  const constraint = constraintByName.get(name)
                  return (
                    <td key={name} className="px-3 py-2">
                      {constraint ? (
                        <span title={constraint.detail}>
                          <ConstraintBadge passed={constraint.passed} />
                        </span>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>
                  )
                })}
                <td className="px-3 py-2 text-slate-600">{design.rationale}</td>
                <Generate3DCell
                  design={design}
                  state={modelState}
                  onGenerate={onGenerate3D}
                />
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default DesignTable
