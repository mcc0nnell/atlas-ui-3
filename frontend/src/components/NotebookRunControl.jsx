import { useRef, useState } from 'react'

const button = 'px-3 py-2 rounded border border-gray-600 hover:bg-gray-700 disabled:opacity-50'

export default function NotebookRunControl({ roomId, artifact, events, disabled, onChange }) {
  const [approved, setApproved] = useState(false)
  const [pending, setPending] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const lock = useRef(false)
  const latest = new Map(events.filter(event => event.type === 'room.notebook.run').map(event => [event.content.run_id, event.content]))
  const active = [...latest.values()].find(run => ['queued', 'running'].includes(run.state))
  const base = `/api/rooms/${encodeURIComponent(roomId)}/notebook-runs`
  async function submit(cancelId) {
    if (lock.current) return
    lock.current = true
    setBusy(true)
    setError('')
    const payload = pending || { run_id: crypto.randomUUID(), artifact_id: artifact.artifact_id,
      revision: artifact.revision, sha256: artifact.sha256, approved: true }
    if (!cancelId) setPending(payload)
    try {
      const response = await fetch(cancelId ? `${base}/${encodeURIComponent(cancelId)}/cancel` : base, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, ...(cancelId ? {} : { body: JSON.stringify(payload) }),
      })
      if (!response.ok) {
        if (!cancelId && [400, 404, 409, 422].includes(response.status)) setPending(null)
        throw new Error(`Notebook request failed (${response.status})`)
      }
      if (!cancelId) { setPending(null); setApproved(false) }
      await onChange()
    } catch (err) { setError(err.message) }
    finally { lock.current = false; setBusy(false) }
  }
  return <section aria-label="Notebook execution" className="border border-gray-600 rounded p-3 space-y-2">
    <h4 className="font-semibold">Run saved notebook</h4>
    <p className="text-sm">Revision {artifact.revision} · SHA-256 <code className="break-all">{artifact.sha256}</code></p>
    <p className="text-xs text-gray-400">Runs all code cells in a fresh isolated kernel through an explicitly started operator worker. The result is a separate notebook. A request does not start a worker.</p>
    <label className="flex gap-2 items-start"><input type="checkbox" checked={approved} disabled={disabled || !!active || !!pending || busy}
      onChange={e => setApproved(e.target.checked)} />I approve execution of this saved revision.</label>
    <button type="button" className={button} disabled={disabled || !!active || busy || (!approved && !pending)}
      onClick={() => submit()}>{pending ? 'Retry run request' : 'Request notebook run'}</button>
    {active && <p role="status">Room run: {active.state}{active.state === 'queued' ? ' · awaiting worker' : ''}</p>}
    {active?.state === 'queued' && <button type="button" className={button} disabled={busy} onClick={() => submit(active.run_id)}>Cancel queued run</button>}
    {error && <p role="alert">{error}</p>}
  </section>
}
