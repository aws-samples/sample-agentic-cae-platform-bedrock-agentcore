import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Tailwind-styled element mapping for chat markdown (parity with the original
 * modules' rich answers): GFM tables, lists, headings, inline/blocks code,
 * links, and blockquotes — all themed via the app's slate/sky tokens so they
 * follow the active display scheme.
 */
const components: Components = {
  p: ({ children }) => <p className="mb-2 last:mb-0 leading-relaxed">{children}</p>,
  h1: ({ children }) => <h1 className="mb-2 mt-1 text-base font-semibold text-slate-900">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 mt-1 text-sm font-semibold text-slate-900">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 mt-1 text-sm font-semibold text-slate-800">{children}</h3>,
  ul: ({ children }) => <ul className="mb-2 ml-4 list-disc space-y-1">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 ml-4 list-decimal space-y-1">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="font-medium text-sky-600 underline underline-offset-2 hover:text-sky-700"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold text-slate-900">{children}</strong>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-slate-300 pl-3 text-slate-600 italic">
      {children}
    </blockquote>
  ),
  code: ({ className, children }) => {
    // Fenced blocks carry a language- className; inline code does not.
    const isBlock = typeof className === 'string' && className.includes('language-')
    if (isBlock) {
      return (
        <code className="block overflow-x-auto rounded bg-slate-800 p-3 font-mono text-xs text-slate-100">
          {children}
        </code>
      )
    }
    return (
      <code className="rounded bg-slate-200 px-1 py-0.5 font-mono text-[0.85em] text-slate-800">
        {children}
      </code>
    )
  },
  pre: ({ children }) => <pre className="my-2">{children}</pre>,
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="w-full border-collapse text-left text-xs">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="text-slate-500">{children}</thead>,
  th: ({ children }) => (
    <th className="border-b border-slate-300 px-2 py-1 font-semibold">{children}</th>
  ),
  td: ({ children }) => <td className="border-b border-slate-200 px-2 py-1">{children}</td>,
}

/**
 * Render trusted assistant markdown text as themed React elements. Uses
 * react-markdown (no raw HTML passthrough), so content is safe from injection.
 */
function Markdown({ children }: { children: string }) {
  return (
    <div className="text-sm text-slate-800">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  )
}

export default Markdown
