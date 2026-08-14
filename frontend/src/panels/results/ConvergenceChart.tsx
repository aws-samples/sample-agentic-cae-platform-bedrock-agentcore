import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ConvergencePoint } from './types'

interface ConvergenceChartProps {
  convergence: readonly ConvergencePoint[]
}

/**
 * Convergence plot (Req 8.1 / 8.2): hypervolume over generations, showing how
 * the Pareto front improves as NSGA-II progresses.
 */
function ConvergenceChart({ convergence }: ConvergenceChartProps) {
  if (convergence.length === 0) {
    return <p className="text-sm text-slate-500">No convergence history available.</p>
  }

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart
        data={convergence as ConvergencePoint[]}
        margin={{ top: 8, right: 16, bottom: 24, left: 8 }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis
          dataKey="generation"
          tick={{ fontSize: 12 }}
          label={{ value: 'Generation', position: 'insideBottom', offset: -12, fontSize: 12 }}
        />
        <YAxis
          tick={{ fontSize: 12 }}
          label={{ value: 'Hypervolume', angle: -90, position: 'insideLeft', fontSize: 12 }}
        />
        <Tooltip />
        <Line
          type="monotone"
          dataKey="hypervolume"
          stroke="#2563eb"
          strokeWidth={2}
          dot={{ r: 3 }}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

export default ConvergenceChart
