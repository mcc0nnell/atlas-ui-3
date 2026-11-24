import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { YNotebook } from '@jupyter/ydoc'
import * as Y from 'yjs'
import LiveNotebook from '../components/LiveNotebook'
import { decodeState, encodeState, editSource } from '../components/notebookCollaboration'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('merges concurrent source edits without whole-cell replacement', () => {
  const original = YNotebook.create({ data: { cells: [{ id: 'cell', cell_type: 'code', source: 'middle', metadata: {}, outputs: [], execution_count: null }] } })
  const a = new YNotebook(), b = new YNotebook()
  for (const doc of [a, b]) Y.applyUpdate(doc.ydoc, Y.encodeStateAsUpdate(original.ydoc))
  editSource(a.cells[0], 'left middle')
  editSource(b.cells[0], 'middle right')
  Y.applyUpdate(a.ydoc, Y.encodeStateAsUpdate(b.ydoc))
  Y.applyUpdate(b.ydoc, Y.encodeStateAsUpdate(a.ydoc))
  expect(a.cells[0].getSource()).toBe('left middle right')
  expect(b.cells[0].getSource()).toBe('left middle right')
  editSource(a.cells[0], 'left 👋 middle right')
  editSource(a.cells[0], 'left 🌍 middle right')
  expect(a.cells[0].getSource()).toBe('left 🌍 middle right')
  for (const doc of [a, b, original]) doc.dispose()
})

it('retains local edits on a failed sync, retries and displays the merged checkpoint', async () => {
  const server = YNotebook.create({ data: { cells: [{ id: 'cell', cell_type: 'markdown', source: 'middle', metadata: {} }] } })
  let revision = 1, fail = false
  vi.stubGlobal('fetch', vi.fn(async (_url, options) => {
    const { update } = JSON.parse(options.body)
    if (fail && update) { fail = false; return { ok: false, status: 503 } }
    const before = JSON.stringify(server.getSource())
    if (update) Y.applyUpdate(server.ydoc, decodeState(update))
    const text = JSON.stringify(server.getSource())
    if (before !== text) revision++
    return { ok: true, json: async () => ({ state: encodeState(Y.encodeStateAsUpdate(server.ydoc)), revision, title: 'Research', text }) }
  }))
  const onComment = vi.fn()
  const a = render(<LiveNotebook roomId="room" artifactId="artifact" onClose={vi.fn()} onComment={onComment} />)
  const b = render(<LiveNotebook roomId="room" artifactId="artifact" onClose={vi.fn()} onComment={vi.fn()} />)
  const left = within(a.container), right = within(b.container)
  await waitFor(() => expect(left.getByLabelText('Live cell 1 · markdown')).toHaveValue('middle'))
  await waitFor(() => expect(right.getByLabelText('Live cell 1 · markdown')).toHaveValue('middle'))
  fireEvent.change(left.getByLabelText('Live cell 1 · markdown'), { target: { value: 'left middle' } })
  fireEvent.change(right.getByLabelText('Live cell 1 · markdown'), { target: { value: 'middle right' } })
  fail = true
  fireEvent.click(left.getByRole('button', { name: 'Synchronize now' }))
  await left.findByText(/Synchronization failed/)
  expect(left.getByLabelText('Live cell 1 · markdown')).toHaveValue('left middle')
  expect(left.getByRole('button', { name: 'Close live editor' })).toBeDisabled()
  fireEvent.click(right.getByRole('button', { name: 'Synchronize now' }))
  await right.findByText('Synchronized · revision 2')
  fireEvent.click(left.getByRole('button', { name: 'Synchronize now' }))
  await left.findByText('Synchronized · revision 3')
  expect(left.getByLabelText('Live cell 1 · markdown')).toHaveValue('left middle right')
  fireEvent.click(right.getByRole('button', { name: 'Synchronize now' }))
  await right.findByText('Synchronized · revision 3')
  expect(right.getByLabelText('Live cell 1 · markdown')).toHaveValue('left middle right')
  fireEvent.click(left.getByRole('button', { name: 'Discuss live cell 1' }))
  expect(onComment).toHaveBeenCalledWith({ artifact_id: 'artifact', revision: 3, cell_id: 'cell' })
  expect(screen.getAllByRole('link', { name: 'Download checkpoint' })).toHaveLength(2)
  a.unmount(); b.unmount(); server.dispose()
})
