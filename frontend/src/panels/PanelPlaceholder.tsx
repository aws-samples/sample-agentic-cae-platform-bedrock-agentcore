interface PanelPlaceholderProps {
  title: string
  description: string
}

/**
 * Shared placeholder shell for the four platform panels. Each panel route
 * (Drawing Viewer, Chat, Results Dashboard, 3D Viewer) renders this until the
 * dedicated implementation tasks (12.4-12.7) replace it with real content.
 */
function PanelPlaceholder({ title, description }: PanelPlaceholderProps) {
  return (
    <section
      className="flex h-full w-full flex-col gap-3 rounded-lg border border-slate-200 bg-slate-50 p-6 shadow-sm"
      aria-labelledby="panel-heading"
    >
      <h1 id="panel-heading" className="text-2xl font-semibold text-slate-900">
        {title}
      </h1>
      <p className="max-w-2xl text-base leading-relaxed text-slate-600">
        {description}
      </p>
    </section>
  )
}

export default PanelPlaceholder
