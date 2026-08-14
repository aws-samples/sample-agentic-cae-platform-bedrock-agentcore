/**
 * Typed model for optimization results rendered by the Results Dashboard panel
 * (Req 5.4, 5.6, 8.1). Mirrors the backend `OptimizationResult` dataclass
 * (design §"Optimization Result Model") reshaped for the frontend: objective
 * values are keyed by name, constraint satisfaction is per-constraint pass/fail,
 * and convergence history is a hypervolume series over generations.
 *
 * Results arrive from the backend via a WebSocket `result` frame (task 10.2),
 * but the panel renders purely from typed props/state so it works in isolation
 * with sample or empty data.
 */

/** One point in the convergence history: hypervolume achieved at a generation. */
export interface ConvergencePoint {
  /** Zero-based (or one-based) generation index reported by NSGA-II. */
  generation: number
  /** Hypervolume indicator at this generation (higher is better). */
  hypervolume: number
}

/** Pass/fail status of a single extracted constraint for a design (Req 5.4). */
export interface ConstraintStatus {
  /** Human-readable constraint name (e.g. "max_temp <= 85C"). */
  name: string
  /** Whether the design satisfies this constraint. */
  passed: boolean
  /** Optional detail (e.g. actual value vs bound) for tooltips. */
  detail?: string
}

/**
 * A single Pareto-optimal design (Req 5.4): numeric objective values, per
 * constraint pass/fail status, and an engineering rationale describing its
 * trade-off positioning on the Pareto front.
 */
export interface ParetoDesign {
  /** Stable identifier used for selection + comparison. */
  id: string
  /** Objective name → numeric value (e.g. `{ mass: 1.2, maxTemp: 78 }`). */
  objectives: Record<string, number>
  /** Per-constraint satisfaction status. */
  constraints: ConstraintStatus[]
  /** Engineering rationale for this design's trade-off positioning. */
  rationale: string
}

/**
 * Full optimization result rendered by the dashboard. `reportUrl` links to a
 * backend-generated Markdown report when present (Req 5.6); otherwise the panel
 * builds one client-side from this structure.
 */
export interface OptimizationResult {
  /** Pareto-optimal designs (up to 20 per Req 5.2). */
  designs: ParetoDesign[]
  /** Hypervolume-over-generation series for the convergence plot (Req 8.2). */
  convergence: ConvergencePoint[]
  /** NSGA-II population size. */
  populationSize: number
  /** Number of generations executed. */
  generationCount: number
  /** Pre-generated Markdown report URL (Req 5.6), if the backend produced one. */
  reportUrl?: string
  /** Pre-generated Markdown report body, used as a fallback download source. */
  reportMarkdown?: string
  /** Source drawing id for traceability (Req 8.5). */
  sourceDrawingId?: string
  /** Extraction id for traceability (Req 8.5). */
  extractionId?: string
}

/**
 * A persisted optimization run with its completion timestamp (Req 2.2).
 * Extends OptimizationResult with the ISO-8601 timestamp from the history table
 * sort key, enabling the History Selector to order and label runs.
 */
export interface OptimizationHistoryEntry extends OptimizationResult {
  /** ISO-8601 timestamp of when the optimization run completed. */
  timestamp: string
}

/**
 * Tracks the 3D model generation lifecycle for a single Pareto design (Req 3.1, 5.2, 5.3).
 * Used by the Results Dashboard to show per-design generation progress and
 * by the Viewer Panel to know when a model URL is available for rendering.
 */
export interface ModelGenerationState {
  /** Stable design identifier linking this state to a ParetoDesign. */
  designId: string
  /** Current generation lifecycle phase. */
  status: 'idle' | 'generating' | 'ready' | 'error'
  /** CloudFront URL for the generated STL model; present when status is 'ready'. */
  modelUrl?: string
  /** Error message from the generation tool; present when status is 'error'. */
  error?: string
}
