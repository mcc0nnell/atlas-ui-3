import { useEffect, useId, useRef, useState } from 'react'
import { YNotebook } from '@jupyter/ydoc'
import * as Y from 'yjs'
import { decodeState, encodeState, editSource } from './notebookCollaboration'
import NotebookOutputs from './NotebookOutputs'

const button = 'px-3 py-2 rounded border border-gray-600 hover:bg-gray-700 disabled:opacity-50'
const input = 'w-full min-w-0 rounded bg-gray-800 border border-gray-600 p-2 text-gray-100'

export default function LiveNotebook({ roomId, artifactId, onClose, onComment }) {
  const editorId = useId()
  const [view, setView] = useState(null)
  const [status, setStatus] = useState('Connecting…')
  const [dirty, setDirty] = useState(false)
  const modelRef = useRef(null)
  const refresh = useRef(null)

  useEffect(() => {
    let stopped = false, inFlight = false, changed = 0, acknowledged = 0
    let model = null, checkpoint = null
    const abort = new AbortController()
    const render = () => {
      if (!stopped && model) {
        setView({ notebook: model.getSource(), revision: checkpoint?.revision, title: checkpoint?.title, text: checkpoint?.text })
        setDirty(changed !== acknowledged)
      }
    }
    const onUpdate = (_update, origin) => {
      if (origin !== 'server') { changed++; setStatus('Local changes pending') }
      render()
    }
    const synchronize = async () => {
      if (stopped || inFlight) return
      inFlight = true
      const submitted = changed
      try {
        const update = model && submitted !== acknowledged ? encodeState(Y.encodeStateAsUpdate(model.ydoc)) : undefined
        const response = await fetch(`/api/rooms/${encodeURIComponent(roomId)}/artifacts/${encodeURIComponent(artifactId)}/collaboration`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ update }), signal: abort.signal,
        })
        if (!response.ok) throw new Error(`Synchronization failed (${response.status})`)
        const result = await response.json()
        if (stopped) return
        if (!model) { model = new YNotebook(); modelRef.current = model; model.ydoc.on('update', onUpdate) }
        Y.applyUpdate(model.ydoc, decodeState(result.state), 'server')
        checkpoint = result
        acknowledged = submitted
        render()
        setStatus(changed === acknowledged ? `Synchronized · revision ${result.revision}` : 'Local changes pending')
      } catch (error) {
        if (!stopped) setStatus(`${error.message}. Local edits are retained in this tab; retry synchronization.`)
      } finally { inFlight = false }
    }
    refresh.current = synchronize
    synchronize()
    const timer = window.setInterval(synchronize, 2000)
    const beforeUnload = event => {
      if (changed !== acknowledged) { event.preventDefault(); event.returnValue = '' }
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => {
      stopped = true
      abort.abort()
      window.clearInterval(timer)
      window.removeEventListener('beforeunload', beforeUnload)
      refresh.current = null
      if (model) { model.ydoc.off('update', onUpdate); model.dispose() }
      modelRef.current = null
    }
  }, [roomId, artifactId])

  const mutate = fn => { if (modelRef.current) fn(modelRef.current) }
  const add = cell_type => mutate(model => model.addCell({ id: crypto.randomUUID(), cell_type, metadata: {}, source: '',
    ...(cell_type === 'code' ? { execution_count: null, outputs: [] } : {}) }))
  return <section aria-label="Live notebook" className="border border-gray-600 rounded p-3 space-y-3">
    <h3 className="font-semibold">Live notebook{view?.title ? ` · ${view.title}` : ''}</h3>
    <p role="status">{status}</p>
    <div className="flex flex-wrap gap-2">
      <button type="button" className={button} onClick={() => refresh.current?.()}>Synchronize now</button>
      <button type="button" className={button} disabled={dirty} onClick={onClose}>Close live editor</button>
    </div>
    {view && <>
      {view.notebook.cells.map((cell, index) => <section key={cell.id} className="border border-gray-600 rounded p-2 space-y-2">
        <label htmlFor={`${editorId}-live-cell-${cell.id}`}>Live cell {index + 1} · {cell.cell_type}</label>
        <textarea id={`${editorId}-live-cell-${cell.id}`} className={input} rows={4} value={cell.source}
          onChange={event => mutate(model => {
            const target = model.cells.find(c => c.id === cell.id)
            if (target) model.transact(() => editSource(target, event.target.value))
          })} />
        <NotebookOutputs cell={cell} />
        <button type="button" className={button} onClick={() => mutate(model => {
          const i = model.cells.findIndex(c => c.id === cell.id)
          if (i >= 0) model.deleteCell(i)
        })}>Remove live cell {index + 1}</button>{' '}
        <button type="button" className={button} disabled={dirty || !view.revision} onClick={() => onComment({ artifact_id: artifactId, revision: view.revision, cell_id: cell.id })}>Discuss live cell {index + 1}</button>
      </section>)}
      <div className="flex flex-wrap gap-2">
        <button type="button" className={button} onClick={() => add('markdown')}>Add live Markdown cell</button>
        <button type="button" className={button} onClick={() => add('code')}>Add live code cell</button>
        <a className={button} download="notebook.ipynb" href={`data:application/json;charset=utf-8,${encodeURIComponent(view.text)}`}>Download checkpoint</a>
      </div>
      <p className="text-xs text-gray-400">Edits synchronize every two seconds. Only acknowledged checkpoints are durable. Code is not executed.</p>
    </>}
  </section>
}
