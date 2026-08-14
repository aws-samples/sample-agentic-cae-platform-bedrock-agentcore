import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import type { ParetoDesign } from './types'

interface ParetoChartProps {
  designs: readonly ParetoDesign[]
  /** Objective plotted on the X axis. */
  xObjective: string
  /** Objective plotted on the Y axis. */
  yObjective: string
  /** Ids currently selected for comparison (rendered as a highlight series). */
  selectedIds: readonly string[]
}

interface ScatterPoint {
  id: string
  x: number
  y: number
}

function toPoints(
  designs: readonly ParetoDesign[],
  xObjective: string,
  yObjective: string,
): ScatterPoint[] {
  return designs
    .filter(
      (design) =>
        typeof design.objectives[xObjective] === 'number' &&
        typeof design.objectives[yObjective] === 'number',
    )
    .map((design) => ({
      id: design.id,
      x: design.objectives[xObjective],
      y: design.objectives[yObjective],
    }))
}

/**
 * Pareto-front scatter plot (Req 8.1): two selected objectives on the X/Y axes,
 * one point per design. Designs chosen for comparison are drawn as a separate
 * highlighted series so the current selection is visible on the front.
 */
function ParetoChart({ designs, xObjective, yObjective, selectedIds }: ParetoChartProps) {
  const points = toPoints(designs, xObjective, yObjective)
  const selectedPoints = points.filter((point) => selectedIds.includes(point.id))

  if (points.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        Need at least two numeric objectives to plot the Pareto front.
      </p>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={280}>
      <ScatterChart margin={{ top: 8, right: 16, bottom: 24, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis
          type="number"
          dataKey="x"
          name={xObjective}
          tick={{ fontSize: 12 }}
          label={{ value: xObjective, position: 'insideBottom', offset: -12, fontSize: 12 }}
        />
        <YAxis
          type="number"
          dataKey="y"
          name={yObjective}
          tick={{ fontSize: 12 }}
          label={{ value: yObjective, angle: -90, position: 'insideLeft', fontSize: 12 }}
        />
        <ZAxis range={[80, 80]} />
        <Tooltip cursor={{ strokeDasharray: '3 3' }} />
        <Scatter name="Pareto designs" data={points} fill="#94a3b8" />
        {selectedPoints.length > 0 && (
          <Scatter name="Selected" data={selectedPoints} fill="#2563eb" />
        )}
      </ScatterChart>
    </ResponsiveContainer>
  )
}

export default ParetoChart
