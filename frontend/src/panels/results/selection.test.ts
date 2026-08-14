import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import { MAX_COMPARISON_DESIGNS, toggleSelection } from './selection'

describe('toggleSelection (Req 6.5 — up to 4 designs)', () => {
  it('adds an id when there is room', () => {
    expect(toggleSelection([], 'D1')).toEqual(['D1'])
    expect(toggleSelection(['D1'], 'D2')).toEqual(['D1', 'D2'])
  })

  it('removes an already-selected id', () => {
    expect(toggleSelection(['D1', 'D2'], 'D1')).toEqual(['D2'])
  })

  it('enforces the cap of 4 by refusing to add a fifth id', () => {
    const full = ['D1', 'D2', 'D3', 'D4']
    expect(toggleSelection(full, 'D5')).toEqual(full)
    expect(toggleSelection(full, 'D5')).toHaveLength(MAX_COMPARISON_DESIGNS)
  })

  it('still allows deselection when at the cap', () => {
    const full = ['D1', 'D2', 'D3', 'D4']
    expect(toggleSelection(full, 'D2')).toEqual(['D1', 'D3', 'D4'])
  })

  it('does not mutate the input array', () => {
    const input = ['D1']
    const output = toggleSelection(input, 'D2')
    expect(input).toEqual(['D1'])
    expect(output).not.toBe(input)
  })

  it('respects a custom max', () => {
    expect(toggleSelection(['D1', 'D2'], 'D3', 2)).toEqual(['D1', 'D2'])
  })

  it('property: the selection length never exceeds the cap', () => {
    fc.assert(
      fc.property(
        fc.array(fc.string({ minLength: 1, maxLength: 4 }), { maxLength: 20 }),
        (ids) => {
          let selected: string[] = []
          for (const id of ids) {
            selected = toggleSelection(selected, id, MAX_COMPARISON_DESIGNS)
            expect(selected.length).toBeLessThanOrEqual(MAX_COMPARISON_DESIGNS)
          }
        },
      ),
    )
  })

  it('property: toggling the same id twice restores the prior selection', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.string({ minLength: 1, maxLength: 4 }), { maxLength: 4 }),
        fc.string({ minLength: 1, maxLength: 4 }),
        (selected, id) => {
          const once = toggleSelection(selected, id, MAX_COMPARISON_DESIGNS)
          const twice = toggleSelection(once, id, MAX_COMPARISON_DESIGNS)
          // When the first toggle changed the selection (add or remove), the
          // second toggle inverts it, returning the original membership.
          if (once.length !== selected.length) {
            expect([...twice].sort()).toEqual([...selected].sort())
          }
        },
      ),
    )
  })

  it('property: the selection never contains duplicates', () => {
    fc.assert(
      fc.property(
        fc.array(fc.string({ minLength: 1, maxLength: 3 }), { maxLength: 30 }),
        (ids) => {
          let selected: string[] = []
          for (const id of ids) {
            selected = toggleSelection(selected, id, MAX_COMPARISON_DESIGNS)
          }
          expect(new Set(selected).size).toBe(selected.length)
        },
      ),
    )
  })
})
