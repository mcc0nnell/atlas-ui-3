export const decodeState = text => Uint8Array.from(atob(text), char => char.charCodeAt(0))
export const encodeState = bytes => btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))

// Preserve untouched spans, so independent edits can merge instead of replacing a whole cell.
export function editSource(cell, next) {
  const before = Array.from(cell.getSource()), after = Array.from(next)
  let start = 0, end = 0
  while (start < before.length && start < after.length && before[start] === after[start]) start++
  while (end < before.length - start && end < after.length - start &&
    before[before.length - end - 1] === after[after.length - end - 1]) end++
  if (start === before.length && start === after.length) return
  cell.updateSource(before.slice(0, start).join('').length,
    before.slice(0, before.length - end).join('').length, after.slice(start, after.length - end).join(''))
  if (cell.cell_type === 'code') { cell.setOutputs([]); cell.execution_count = null }
}
