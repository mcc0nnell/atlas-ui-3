import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import NotebookRunControl from '../components/NotebookRunControl'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('requires approval of an exact revision and reuses a request ID on retry', async () => {
  const artifact = { artifact_id: 'notebook', revision: 3, sha256: 'a'.repeat(64) }
  const fetcher = vi.fn().mockRejectedValueOnce(new Error('Lost response')).mockResolvedValue({ ok: true })
  vi.stubGlobal('fetch', fetcher)
  const onChange = vi.fn()
  render(<NotebookRunControl roomId="room" artifact={artifact} events={[]} onChange={onChange} />)
  expect(screen.getByRole('button', {name:'Request notebook run'})).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', {name:'Request notebook run'}))
  fireEvent.click(await screen.findByRole('button', {name:'Retry run request'}))
  await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1))
  expect(fetcher.mock.calls[0][1].body).toBe(fetcher.mock.calls[1][1].body)
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ artifact_id: 'notebook', revision: 3, sha256: artifact.sha256, approved: true })
})
it('restores queued state from the timeline and allows cancellation', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok:true}))
  const onChange = vi.fn()
  render(<NotebookRunControl roomId="room" artifact={{artifact_id:'nb', revision:1, sha256:'a'.repeat(64)}} events={[{type:'room.notebook.run',content:{run_id:'run',state:'queued'}}]} onChange={onChange} />)
  expect(screen.getByRole('button', {name:'Request notebook run'})).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent('awaiting worker')
  fireEvent.click(screen.getByRole('button', {name:'Cancel queued run'}))
  await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1))
  expect(fetch).toHaveBeenCalledWith('/api/rooms/room/notebook-runs/run/cancel', expect.objectContaining({method:'POST'}))
})
