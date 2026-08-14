import type { ParetoDesign } from './types'

interface DesignComparisonProps {
  /** The designs selected for side-by-side comparison (2 or more). */
  designs: readonly ParetoDesign[]
  /** Objective names to compare, in display order. */
  objectiveNames: readonly string[]
  /** Constraint names to compare, in display order. */
  constraintNames: readonly string[]
}

/**
 * Determine, per objective, which selected design holds the best (lowest) value
 * so it can be highlighted. Optimization objectives here are minimized
 * (mass, temperature, cost, etc.), matching the original SimAgent comparison.
 */
function bestByObjective(
  designs: readonly ParetoDesign[],
  objectiveNames: readonly string[],
): Record<string, string | null> {
  const best: Record<string, string | null> = {}
  for (const name of objectiveNames) {
    let bestId: string | null = null
    let bestVal = Number.POSITIVE_INFINITY
    for (const design of designs) {
      const value = design.objectives[name]
      if (typeof value === 'number' && value < bestVal) {
        bestVal = value
        bestId = design.id
      }
    }
    best[name] = bestId
  }
  return best
}

/**
 * Inline side-by-side comparison of the selected Pareto designs (parity with
 * the original SimAgent DesignComparison): objectives and constraints as rows,
 * one column per selected design, with the best value in each objective row
 * highlighted.
 */
function DesignComparison({
  designs,
  objectiveNames,
  constraintNames,
}: DesignComparisonProps) {
  if (designs.length < 2) {
    return (
      <p className="rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-500">
        Select two or more designs in the table above to compare them
        side-by-side.
      </p>
    )
  }

  const best = bestByObjective(designs, objectiveNames)

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <th scope="col" className="px-3 py-2 font-medium">
              Metric
            </th>
            {designs.map((d) => (
              <th key={d.id} scope="col" className="px-3 py-2 font-medium text-slate-700">
                {d.id}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {objectiveNames.map((name) => (
            <tr key={name} className="border-b border-slate-100">
              <th scope="row" className="px-3 py-2 text-left font-medium text-slate-600">
                {name}
              </th>
              {designs.map((d) => {
                const value = d.objectives[name]
                const isBest = best[name] === d.id
                return (
                  <td
                    key={d.id}
                    className={
                      isBest
                        ? 'px-3 py-2 tabular-nums font-semibold text-emerald-700'
                        : 'px-3 py-2 tabular-nums text-slate-700'
                    }
                  >
                    {typeof value === 'number' ? value : '—'}
                    {isBest && (
                      <span className="ml-1 text-xs font-normal text-emerald-600">
                        best
                      </span>
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
          {constraintNames.map((name) => (
            <tr key={name} className="border-b border-slate-100">
              <th scope="row" className="px-3 py-2 text-left font-medium text-slate-600">
                {name}
              </th>
              {designs.map((d) => {
                const constraint = d.constraints.find((c) => c.name === name)
                return (
                  <td key={d.id} className="px-3 py-2" title={constraint?.detail}>
                    {constraint ? (
                      <span
                        className={
                          constraint.passed
                            ? 'inline-flex rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700'
                            : 'inline-flex rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700'
                        }
                      >
                        {constraint.passed ? 'Pass' : 'Fail'}
                      </span>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
          <tr>
            <th scope="row" className="px-3 py-2 text-left font-medium text-slate-600">
              Rationale
            </th>
            {designs.map((d) => (
              <td key={d.id} className="px-3 py-2 text-xs text-slate-500">
                {d.rationale}
              </td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  )
}

export default DesignComparison
