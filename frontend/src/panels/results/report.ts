import type { OptimizationResult, ParetoDesign } from './types'

/**
 * Pure builder for the downloadable optimization Markdown report (Req 5.6).
 *
 * Produces a self-contained Markdown document covering the objectives, the
 * extracted constraints (with the source drawing reference when known), the
 * population size and generation count, the convergence metrics, and the final
 * Pareto-optimal design set with objective values. Used as the download source
 * when the backend did not supply a `reportUrl`/`reportMarkdown`.
 */
export function buildReportMarkdown(result: OptimizationResult): string {
  const lines: string[] = []

  lines.push('# Optimization Report')
  lines.push('')
  if (result.sourceDrawingId) {
    lines.push(`- **Source drawing:** ${result.sourceDrawingId}`)
  }
  if (result.extractionId) {
    lines.push(`- **Extraction:** ${result.extractionId}`)
  }
  lines.push(`- **Population size:** ${result.populationSize}`)
  lines.push(`- **Generation count:** ${result.generationCount}`)
  lines.push('')

  lines.push('## Objectives')
  const objectiveNames = collectObjectiveNames(result.designs)
  if (objectiveNames.length === 0) {
    lines.push('_No objectives recorded._')
  } else {
    for (const name of objectiveNames) {
      lines.push(`- ${name}`)
    }
  }
  lines.push('')

  lines.push('## Constraints')
  const constraintNames = collectConstraintNames(result.designs)
  if (constraintNames.length === 0) {
    lines.push('_No constraints recorded._')
  } else {
    const ref = result.sourceDrawingId
      ? ` _(source drawing ${result.sourceDrawingId})_`
      : ''
    for (const name of constraintNames) {
      lines.push(`- ${name}${ref}`)
    }
  }
  lines.push('')

  lines.push('## Convergence Metrics')
  if (result.convergence.length === 0) {
    lines.push('_No convergence history recorded._')
  } else {
    const first = result.convergence[0]
    const last = result.convergence[result.convergence.length - 1]
    lines.push(`- Generations recorded: ${result.convergence.length}`)
    lines.push(
      `- Initial hypervolume (generation ${first.generation}): ${first.hypervolume}`,
    )
    lines.push(
      `- Final hypervolume (generation ${last.generation}): ${last.hypervolume}`,
    )
  }
  lines.push('')

  lines.push('## Pareto Set')
  if (result.designs.length === 0) {
    lines.push('_No Pareto-optimal designs found._')
  } else {
    for (const design of result.designs) {
      lines.push(`### Design ${design.id}`)
      const objectives = Object.entries(design.objectives)
        .map(([key, value]) => `${key} = ${value}`)
        .join(', ')
      lines.push(`- Objectives: ${objectives || 'n/a'}`)
      const constraints = design.constraints
        .map((constraint) => `${constraint.name}: ${constraint.passed ? 'PASS' : 'FAIL'}`)
        .join(', ')
      lines.push(`- Constraints: ${constraints || 'n/a'}`)
      lines.push(`- Rationale: ${design.rationale || 'n/a'}`)
      lines.push('')
    }
  }

  return lines.join('\n')
}

/** Ordered union of objective names across all designs (first-seen order). */
export function collectObjectiveNames(designs: readonly ParetoDesign[]): string[] {
  const names: string[] = []
  for (const design of designs) {
    for (const name of Object.keys(design.objectives)) {
      if (!names.includes(name)) {
        names.push(name)
      }
    }
  }
  return names
}

/** Ordered union of constraint names across all designs (first-seen order). */
export function collectConstraintNames(designs: readonly ParetoDesign[]): string[] {
  const names: string[] = []
  for (const design of designs) {
    for (const constraint of design.constraints) {
      if (!names.includes(constraint.name)) {
        names.push(constraint.name)
      }
    }
  }
  return names
}
