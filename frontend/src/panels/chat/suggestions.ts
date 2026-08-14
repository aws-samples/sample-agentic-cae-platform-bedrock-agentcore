/**
 * Pure helpers for chat suggestion chips (parity with the original modules'
 * clickable follow-up suggestions).
 *
 * Assistant answers may end with a short list of suggested follow-up queries,
 * each on its own line as a quoted bullet, e.g.:
 *
 *   - "What is the material of part 3?"
 *   * "Compare the two mounting options"
 *
 * {@link extractSuggestions} pulls those quoted strings out so the UI can
 * render them as one-click chips, and {@link stripSuggestionLines} removes
 * them from the rendered answer body so they aren't shown twice.
 */

/** Matches a bullet line containing a quoted suggestion. */
const SUGGESTION_LINE = /^\s*[-*]\s*"([^"]{2,200})"\s*$/

/**
 * Extract quoted follow-up suggestions from assistant `text`. Returns an
 * ordered, de-duplicated list (max 6) of the inner query strings; empty when
 * none are present.
 */
export function extractSuggestions(text: string): string[] {
  const out: string[] = []
  for (const line of text.split('\n')) {
    const match = SUGGESTION_LINE.exec(line)
    if (match) {
      const query = match[1].trim()
      if (query.length > 0 && !out.includes(query)) {
        out.push(query)
      }
    }
    if (out.length >= 6) break
  }
  return out
}

/**
 * Remove the quoted-suggestion bullet lines from `text` so the answer body and
 * the chips don't duplicate each other. Preserves all other content and
 * trims trailing blank lines.
 */
export function stripSuggestionLines(text: string): string {
  const kept = text.split('\n').filter((line) => !SUGGESTION_LINE.test(line))
  return kept.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd()
}

/** Starter prompts shown in the empty/welcome state to get users going. */
export const STARTER_SUGGESTIONS: readonly string[] = [
  'What is the material of part 3?',
  'List all GD&T callouts in this drawing',
  'What are the critical tolerances?',
  'Optimize this design to minimize mass and cost',
]
