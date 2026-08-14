/**
 * Pure selection helper for side-by-side design comparison (Req 6.5 / task 12.6).
 *
 * The Results Dashboard lets an engineer pick designs from the Pareto set for a
 * side-by-side 3D comparison, capped at a maximum (4 per Req 6.5). This helper
 * centralises the toggle + cap logic so it can be unit and property tested
 * independently of React state.
 */

/** Default maximum number of designs selectable for comparison (Req 6.5). */
export const MAX_COMPARISON_DESIGNS = 4

/**
 * Toggle `id` within the current `selected` list, enforcing a maximum size.
 *
 * - If `id` is already selected, it is removed (always allowed, even at the cap).
 * - If `id` is not selected and there is room, it is appended.
 * - If `id` is not selected and the cap is reached, the selection is returned
 *   unchanged (the cap is never exceeded).
 *
 * Always returns a new array; never mutates the input.
 */
export function toggleSelection(
  selected: readonly string[],
  id: string,
  max: number = MAX_COMPARISON_DESIGNS,
): string[] {
  if (selected.includes(id)) {
    return selected.filter((current) => current !== id)
  }
  if (selected.length >= max) {
    return [...selected]
  }
  return [...selected, id]
}
