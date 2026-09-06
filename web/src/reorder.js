// Drag-to-reorder maths, kept out of the component so it can be tested directly.
//
// Two bugs lived here while it was inline: the order was computed from a stale render
// closure and then applied against fresh state (which could produce holes in the
// list), and the insert always went BEFORE the target, so every downward drag landed
// one position short of where it was dropped.

/**
 * The new id order after dragging `from` onto `target`.
 * Returns null when the move is a no-op or either id is no longer in the list —
 * the caller should then leave the current order alone.
 */
export function reorderIds(ids, from, target) {
  if (!from || !target || from === target) return null
  const fromAt = ids.indexOf(from)
  const targetAt = ids.indexOf(target)
  if (fromAt === -1 || targetAt === -1) return null

  const next = ids.filter((id) => id !== from)
  const at = next.indexOf(target)
  // Dropping onto a page BELOW lands after it; onto one above, before it. That is
  // what makes the page end up where it was actually dropped.
  return [...next.slice(0, at + (fromAt < targetAt ? 1 : 0)), from,
          ...next.slice(at + (fromAt < targetAt ? 1 : 0))]
}
