import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'

const LiveNotebook = lazy(() => import('./LiveNotebook'))
import { Link } from 'react-router-dom'
import { useChat } from '../contexts/ChatContext'
import NotebookOutputs from './NotebookOutputs'
import NotebookRunControl from './NotebookRunControl'
import { notebookReview, reviewPrompt } from './notebookReview'

const button = 'px-3 py-2 rounded border border-gray-600 hover:bg-gray-700 disabled:opacity-50'
const input = 'w-full min-w-0 rounded bg-gray-800 border border-gray-600 p-2 text-gray-100'

async function request(path, options) {
  const response = await fetch(path, options)
  if (!response.ok) {
    const error = new Error(`Room request failed (${response.status})`)
    error.status = response.status
    throw error
  }
  return response.json()
}

function post(path, payload) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

function emptyNotebook() {
  return { nbformat: 4, nbformat_minor: 5, metadata: {}, cells: [] }
}

function NotebookEditor({ text, onChange, disabled, onComment, committed }) {
  let notebook
  try { notebook = JSON.parse(text) } catch { notebook = null }
  if (!notebook || !Array.isArray(notebook.cells)) return <p role="alert">Invalid notebook content.</p>
  const update = cells => onChange(JSON.stringify({ ...notebook, cells }))
  const add = cell_type => update([...notebook.cells, { id: crypto.randomUUID(), cell_type,
    metadata: {}, source: '', ...(cell_type === 'code' ? { execution_count: null, outputs: [] } : {}) }])
  return <div className="space-y-3" aria-label="Notebook cells">
    {notebook.cells.map((cell, index) => <section key={cell.id} className="border border-gray-600 rounded p-2 space-y-2">
      <label htmlFor={`notebook-cell-${cell.id}`}>Cell {index + 1} · {cell.cell_type}</label>
      <textarea id={`notebook-cell-${cell.id}`} className={input} rows={4} disabled={disabled}
        value={Array.isArray(cell.source) ? cell.source.join('') : cell.source}
        onChange={e => update(notebook.cells.map(c => c.id === cell.id ? { ...c, source: e.target.value,
          ...(c.cell_type === 'code' ? { execution_count: null, outputs: [] } : {}) } : c))} />
      <NotebookOutputs cell={cell} />
      <div className="flex flex-wrap gap-2">
        <button type="button" className={button} disabled={disabled} onClick={() => update(notebook.cells.filter(c => c.id !== cell.id))}>Remove cell {index + 1}</button>
        <button type="button" className={button} disabled={disabled || !committed} onClick={() => onComment(cell.id)}>Discuss cell {index + 1}</button>
      </div>
    </section>)}
    <div className="flex flex-wrap gap-2">
      <button type="button" className={button} disabled={disabled} onClick={() => add('markdown')}>Add Markdown cell</button>
      <button type="button" className={button} disabled={disabled} onClick={() => add('code')}>Add code cell</button>
    </div>
    <p className="text-xs text-gray-400">Cells are saved as standard notebook JSON. This editor does not execute code; saved outputs are untrusted artifact content.</p>
  </div>
}

function RoomTimeline({ room, models }) {
  const path = `/api/rooms/${encodeURIComponent(room.id)}/events`
  const [events, setEvents] = useState([])
  const [participants, setParticipants] = useState([])
  const cursor = useRef(0)
  const loading = useRef(false)
  const sending = useRef(false)
  const [busy, setBusy] = useState(false)
  const [more, setMore] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [cellReference, setCellReference] = useState(null)
  const [agent, setAgent] = useState('')
  const [participant, setParticipant] = useState('')
  const [model, setModel] = useState('')
  const [question, setQuestion] = useState('')
  const [turnPending, setTurnPending] = useState(null)
  const [turnBusy, setTurnBusy] = useState(false)
  const turnLock = useRef(false)
  const [pending, setPending] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [artifact, setArtifact] = useState(null)
  const [artifactTitle, setArtifactTitle] = useState('')
  const [artifactKind, setArtifactKind] = useState('document')
  const [artifactText, setArtifactText] = useState('')
  const [artifactLoading, setArtifactLoading] = useState(false)
  const [liveArtifact, setLiveArtifact] = useState(null)
  const artifactRequest = useRef(0)
  const [reviewSelection, setReviewSelection] = useState(null)
  const availableReview = !artifactLoading && artifact && artifactText === artifact.text &&
    artifactTitle === artifact.title && !liveArtifact ? notebookReview(artifact, events) : null
  const selectedReview = availableReview && reviewSelection === availableReview.key + ':' + model
    ? availableReview : null
  const outgoingPrompt = reviewPrompt(question, selectedReview)

  const resetArtifact = () => {
    artifactRequest.current += 1
    setReviewSelection(null)
    setArtifactLoading(false)
    setArtifact(null)
    setArtifactTitle('')
    setArtifactKind('document')
    setArtifactText('')
  }

  const viewArtifact = async content => {
    const requestId = ++artifactRequest.current
    setReviewSelection(null)
    setArtifactLoading(true)
    setError('')
    try {
      const revision = await request(`/api/rooms/${encodeURIComponent(room.id)}/artifacts/${encodeURIComponent(content.artifact_id)}/revisions/${content.revision}`)
      if (requestId !== artifactRequest.current) return
      setArtifact(revision)
      setArtifactTitle(revision.title)
      setArtifactKind(revision.kind)
      setArtifactText(revision.text)
    } catch (err) {
      if (requestId === artifactRequest.current) setError(err.message)
    } finally {
      if (requestId === artifactRequest.current) setArtifactLoading(false)
    }
  }

  const sync = useCallback(async () => {
    if (loading.current) return
    loading.current = true
    setBusy(true)
    try {
      const page = await request(`${path}?after=${cursor.current}&limit=100`)
      const fresh = page.events.filter(event => event.sequence > cursor.current)
      cursor.current = Math.max(cursor.current, page.next_after)
      setEvents(previous => [...previous, ...fresh])
      setMore(page.has_more)
      const roster = await request(path.replace(/\/events$/, '/participants'))
      setParticipants(roster.participants || [])
    } catch (err) {
      setError(err.message)
    } finally {
      loading.current = false
      setBusy(false)
    }
  }, [path])

  useEffect(() => {
    sync()
    const timer = window.setInterval(sync, 5000)
    return () => window.clearInterval(timer)
  }, [sync])

  const append = async event => {
    if (sending.current || loading.current) return
    sending.current = true
    setSubmitting(true)
    setError('')
    try {
      const payload = pending || { transaction_id: crypto.randomUUID(), event }
      setPending(payload)
      await post(path, payload)
      setPending(null)
      if (['room.message', 'room.notebook.comment'].includes(payload.event.type)) { setMessage(''); setCellReference(null) }
      if (payload.event.type === 'room.agent.added') setAgent('')
      if (payload.event.type === 'room.artifact.revised') resetArtifact()
      await sync()
    } catch (err) {
      if ([404, 422].includes(err.status)) {
        setPending(null)
        setError(err.message)
      } else if (err.status === 409 && (pending?.event || event).type === 'room.artifact.revised') {
        setPending(null)
        setError('Artifact conflict. Refresh and open the latest revision before saving again.')
      } else setError(err.message)
    } finally {
      sending.current = false
      setSubmitting(false)
    }
  }

  const ask = async () => {
    if (turnLock.current || loading.current || (!turnPending && !outgoingPrompt)) return
    turnLock.current = true
    setTurnBusy(true)
    setError('')
    const payload = turnPending || {
      transaction_id: crypto.randomUUID(), agent_id: participant, model, prompt: outgoingPrompt,
    }
    setTurnPending(payload)
    try {
      const result = await post(`/api/rooms/${encodeURIComponent(room.id)}/turns`, payload)
      if (result.state !== 'pending') {
        setTurnPending(null)
        setQuestion('')
        setReviewSelection(null)
        if (result.state === 'failed') setError('Agent turn failed. No reply was saved.')
      }
      await sync()
    } catch (err) {
      setError(err.message)
    } finally {
      turnLock.current = false
      setTurnBusy(false)
    }
  }

  const newerArtifact = artifact && events.filter(event => event.type === 'room.artifact.revised' &&
    event.content.artifact_id === artifact.artifact_id && event.content.revision > artifact.revision).at(-1)?.content
  const participantInfo = new Map(participants.map(p => [p.agent_id, p]))
  const roster = new Set()
  for (const event of events) {
    if (event.type === 'room.agent.added') roster.add(event.content.agent_id)
    if (event.type === 'room.agent.removed') roster.delete(event.content.agent_id)
  }

  return (
    <section className="flex-1 min-w-0 flex flex-col gap-4 p-4" aria-label="Room discussion">
      <div className="flex flex-wrap justify-between items-center gap-2">
        <h2 className="text-xl font-semibold break-all">{room.name}</h2>
        <button className={button} disabled={busy} onClick={() => { setError(''); sync() }}>
          {busy ? 'Loading…' : more ? 'Load more events' : 'Refresh timeline'}
        </button>
      </div>
      {error && <p role="alert" className="text-red-300">{error}</p>}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_15rem]">
        <div>
          <h3 className="font-semibold mb-2">Timeline</h3>
          <ol aria-label="Messages" className="space-y-3 min-h-24">
            {events.map(event => (
              <li key={event.sequence} className="border border-gray-700 rounded p-3 break-words">
                <div className="text-xs text-gray-400">{event.type === 'room.agent.reply'
                  ? `${event.content.agent_id} · ${event.content.model}` : event.sender} · #{event.sequence}</div>
                {['room.message', 'room.agent.reply'].includes(event.type) ? (
                  <p className="whitespace-pre-wrap">{event.content.text}</p>
                ) : event.type === 'room.notebook.comment' ? (
                  <div>
                    <p className="whitespace-pre-wrap">{event.content.text}</p>
                    <button className={button} disabled={!!pending || submitting || artifactLoading}
                      onClick={() => viewArtifact(event.content)}>Open referenced cell {event.content.cell_id} · revision {event.content.revision}</button>
                  </div>
                ) : ['room.agent.added', 'room.agent.removed'].includes(event.type) ? (
                  <p>{event.type === 'room.agent.added' ? 'Added' : 'Removed'} roster entry: {event.content.agent_id}</p>
                ) : event.type === 'room.notebook.run' ? (
                  <div>
                    <p>Notebook run {event.content.state} · input revision {event.content.revision}</p>
                    {event.content.result_artifact_id && <button className={button} disabled={!!pending || submitting || artifactLoading}
                      onClick={() => viewArtifact({ artifact_id: event.content.result_artifact_id, revision: 1 })}>Open executed notebook</button>}
                  </div>
                ) : event.type === 'room.artifact.revised' ? (
                  <div>
                    <p>{event.content.title} · {event.content.kind} · revision {event.content.revision}</p>
                    <button className={button} disabled={!!pending || submitting || artifactLoading}
                      onClick={() => viewArtifact(event.content)}>Open {event.content.title} revision {event.content.revision}</button>
                  </div>
                ) : <p>Room event: {event.type}</p>}
              </li>
            ))}
          </ol>
          {!events.length && !busy && <p className="text-gray-400">No messages yet.</p>}
          <form className="mt-4 space-y-2" onSubmit={e => { e.preventDefault(); append(cellReference ? { type: 'room.notebook.comment', ...cellReference, text: message.trim() } : { type: 'room.message', text: message.trim() }) }}>
            {cellReference && <div className="flex flex-wrap gap-2 items-center"><span>Discussing cell {cellReference.cell_id} · revision {cellReference.revision}</span>
              <button type="button" className={button} disabled={!!pending || submitting} onClick={() => setCellReference(null)}>Clear cell reference</button></div>}
            <label htmlFor="room-message">Message</label>
            <textarea id="room-message" className={input} rows={3} value={message}
              maxLength={16000} disabled={!!pending || submitting} onChange={e => setMessage(e.target.value)} />
            <button className={button} disabled={!message.trim() || !!pending || submitting || busy}>Send message</button>
          </form>
          <section className="mt-6 border border-gray-700 rounded p-3 space-y-2" aria-label="Room artifacts">
            <h3 className="font-semibold">Artifacts</h3>
            <p className="text-sm text-gray-400">Save inert documents, notebooks, workflow definitions, or results. Revisions are immutable; nothing here executes.</p>
            <button className={button} disabled={!!pending || submitting} onClick={resetArtifact}>New artifact</button>
            {artifact && <p>Editing revision {artifact.revision}. Saving creates a new revision.</p>}
            {newerArtifact && <div role="status" className="space-y-2">
              <p>Revision {newerArtifact.revision} is available. Your current draft is unchanged.</p>
              <button type="button" className={button} disabled={!!pending || submitting || artifactLoading}
                onClick={() => { if (artifactText === artifact.text && artifactTitle === artifact.title || window.confirm('Discard local draft and open the latest revision?')) viewArtifact(newerArtifact) }}>Open latest revision</button>
            </div>}
            <form className="space-y-2" onSubmit={e => {
              e.preventDefault()
              append({ type: 'room.artifact.revised', artifact_id: artifact?.artifact_id || crypto.randomUUID(),
                base_revision: artifact?.revision || 0, kind: artifactKind, title: artifactTitle.trim(), text: artifactText })
            }}>
              <label htmlFor="artifact-title">Artifact title</label>
              <input id="artifact-title" className={input} maxLength={200} value={artifactTitle}
                disabled={!!pending || submitting || artifactLoading} onChange={e => setArtifactTitle(e.target.value)} />
              <label htmlFor="artifact-kind">Artifact type</label>
              <select id="artifact-kind" className={input} value={artifactKind}
                disabled={!!artifact || !!pending || submitting || artifactLoading} onChange={e => {
                  setArtifactKind(e.target.value)
                  if (e.target.value === 'notebook') setArtifactText(JSON.stringify(emptyNotebook()))
                  else if (artifactKind === 'notebook') setArtifactText('')
                }}>
                <option value="document">Document</option>
                <option value="workflow">Workflow</option>
                <option value="result">Result</option>
                <option value="notebook">Jupyter notebook</option>
              </select>
              {artifactKind === 'notebook' ? <NotebookEditor text={artifactText} onChange={setArtifactText}
                disabled={!!pending || submitting || artifactLoading} committed={!!artifact && artifactText === artifact.text}
                onComment={cell_id => setCellReference({ artifact_id: artifact.artifact_id, revision: artifact.revision, cell_id })} /> : <>
                <label htmlFor="artifact-text">Artifact content</label>
                <textarea id="artifact-text" className={input} rows={6} maxLength={64000} value={artifactText}
                  disabled={!!pending || submitting || artifactLoading} onChange={e => setArtifactText(e.target.value)} />
              </>}
              {artifactKind === 'notebook' && artifact && <button type="button" className={button}
                disabled={!!liveArtifact || artifactText !== artifact.text || artifactTitle !== artifact.title || artifactLoading}
                onClick={() => setLiveArtifact(artifact.artifact_id)}>Open live notebook</button>}
              {artifactKind === 'notebook' && artifact && <a className={button} download="notebook.ipynb"
                href={`data:application/json;charset=utf-8,${encodeURIComponent(artifact.text)}`}>Download saved notebook</a>}
              {artifactText.length > 64000 && <p role="alert">Artifact exceeds the 64,000 character limit.</p>}
              <button className={button} disabled={!artifactTitle.trim() || artifactText.length > 64000 || (artifact && liveArtifact === artifact.artifact_id) || !!pending || submitting || busy || artifactLoading}>Save artifact revision</button>
            </form>
          </section>
          {artifact?.kind === 'notebook' && artifact.execution_enabled && <NotebookRunControl
            key={`${artifact.artifact_id}:${artifact.revision}`} roomId={room.id} artifact={artifact} events={events}
            disabled={!!liveArtifact || artifactText !== artifact.text || artifactTitle !== artifact.title || artifactLoading || busy}
            onChange={sync} />}
          {liveArtifact && <Suspense fallback={<p role="status">Loading live notebook…</p>}>
            <LiveNotebook key={liveArtifact} roomId={room.id} artifactId={liveArtifact}
              onClose={() => { setLiveArtifact(null); sync() }} onComment={setCellReference} />
          </Suspense>}
        </div>
        <aside className="border border-gray-700 rounded p-3 space-y-3">
          <h3 className="font-semibold">Agent roster</h3>
          <p className="text-xs text-gray-400">Select a roster entry and a model to request a text-only response.{more && ' Load remaining events for the complete roster.'}</p>
          <ul className="space-y-2">
            {[...roster].sort().map(id => <li key={id} className="flex flex-wrap items-center justify-between gap-2">
              <span className="break-all">{id}
                {participantInfo.has(id) && <span className="block text-xs text-gray-400">
                  {participantInfo.get(id).context_turns} remembered turns
                </span>}
              </span>
              <button className={button} aria-label={`Remove ${id}`} disabled={!!pending || submitting || more || busy}
                onClick={() => append({ type: 'room.agent.removed', agent_id: id })}>Remove</button>
            </li>)}
          </ul>
          <form className="space-y-2" onSubmit={e => { e.preventDefault(); append({ type: 'room.agent.added', agent_id: agent.trim() }) }}>
            <label htmlFor="room-agent">Agent name</label>
            <input id="room-agent" className={input} maxLength={200} value={agent}
              disabled={!!pending || submitting} onChange={e => setAgent(e.target.value)} />
            <button className={button} disabled={!agent.trim() || !!pending || submitting || busy}>Add roster entry</button>
          </form>
          <form className="space-y-2 border-t border-gray-700 pt-3" onSubmit={e => { e.preventDefault(); ask() }}>
            <label htmlFor="room-participant">Participant</label>
            <select id="room-participant" className={input} value={participant} disabled={!!turnPending}
              onChange={e => {
                const id = e.target.value
                setReviewSelection(null)
                setParticipant(id)
                const remembered = participantInfo.get(id)?.last_model
                setModel(models.includes(remembered) ? remembered : '')
              }}>
              <option value="">Choose agent</option>
              {[...roster].sort().map(id => <option key={id} value={id}>{id}</option>)}
            </select>
            <label htmlFor="room-model">Model</label>
            <select id="room-model" className={input} value={model} disabled={!!turnPending}
              onChange={e => { setReviewSelection(null); setModel(e.target.value) }}>
              <option value="">Choose model</option>
              {models.map(name => <option key={name} value={name}>{name}</option>)}
            </select>
            <label htmlFor="room-question">Ask agent</label>
            <textarea id="room-question" className={input} rows={3} maxLength={4000}
              value={question} disabled={!!turnPending} onChange={e => setQuestion(e.target.value)} />
            {availableReview && <div className="space-y-2">
              <label className="flex gap-2 items-center">
                <input type="checkbox" checked={!!selectedReview} disabled={!!turnPending || !model}
                  onChange={e => setReviewSelection(e.target.checked ? availableReview.key + ':' + model : null)} />
                Include saved notebook output in this model request
              </label>
              {selectedReview && <>
                <p className="text-xs">The selected model receives the following exact output data and provenance. This request does not execute the notebook. The question and selected data are saved in participant history and may be included in later turns.</p>
                <pre className="whitespace-pre-wrap break-all text-xs" aria-label="Notebook output sent to model">{selectedReview.data}</pre>
                {!outgoingPrompt && question.trim() && <p role="alert">Question and notebook output exceed the 4,000 character limit.</p>}
              </>}
            </div>}
            <button className={button} disabled={turnBusy || busy || more || (!turnPending &&
              (!roster.has(participant) || !models.includes(model) || !outgoingPrompt))}>
              {turnBusy ? 'Waiting for reply…' : turnPending ? 'Check or retry turn' : 'Request response'}
            </button>
            {turnPending && <p role="status">Turn requested. Checking again will not launch another turn.</p>}
          </form>
        </aside>
      </div>
      {pending && !submitting && <div className="space-y-2">
        <p role="status">Delivery not confirmed. Retry uses the same transaction ID.</p>
        <button className={button} disabled={busy} onClick={() => append(pending.event)}>Retry pending event</button>
      </div>}
    </section>
  )
}

export default function RoomsPage() {
  const { models: configuredModels } = useChat()
  const models = [...new Set((configuredModels || []).map(m => typeof m === 'string' ? m : m.name).filter(Boolean))]
  const [rooms, setRooms] = useState([])
  const [selected, setSelected] = useState(null)
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const createLock = useRef(false)

  useEffect(() => {
    let active = true
    request('/api/rooms').then(data => {
      if (active) setRooms(previous => [...previous, ...data.rooms.filter(room => !previous.some(r => r.id === room.id))])
    }).catch(err => { if (active) setError(err.message) })
    return () => { active = false }
  }, [])

  const create = async e => {
    e.preventDefault()
    if (createLock.current || !name.trim()) return
    createLock.current = true
    setCreating(true)
    setError('')
    try {
      const room = await post('/api/rooms', { name: name.trim() })
      setRooms(previous => [room, ...previous.filter(r => r.id !== room.id)])
      setSelected(room.id)
      setName('')
    } catch (err) {
      setError(err.message)
    } finally {
      createLock.current = false
      setCreating(false)
    }
  }

  const room = rooms.find(r => r.id === selected)
  return (
    <main className="min-h-full bg-gray-900 text-gray-200">
      <header className="flex flex-wrap items-center gap-4 border-b border-gray-700 p-4">
        <Link to="/" className={button}>Back to chat</Link>
        <h1 className="text-xl font-semibold">Rooms</h1>
        <span className="text-sm text-gray-400">Experimental · Private · Server-saved</span>
      </header>
      {error && <p role="alert" className="p-4 text-red-300">{error}</p>}
      <div className="flex flex-col md:flex-row">
        <nav aria-label="Rooms" className="md:w-64 md:flex-shrink-0 border-b md:border-b-0 md:border-r border-gray-700 p-4 space-y-3">
          <form onSubmit={create} className="space-y-2">
            <label htmlFor="room-name">Room name</label>
            <input id="room-name" value={name} maxLength={200} className={input} onChange={e => setName(e.target.value)} />
            <button className={button} disabled={creating || !name.trim()}>Create room</button>
          </form>
          <ul className="space-y-1">{rooms.map(r => <li key={r.id}>
            <button className={`${button} w-full text-left break-all`} aria-pressed={selected === r.id}
              onClick={() => setSelected(r.id)}>{r.name}</button>
          </li>)}</ul>
        </nav>
        {room ? <RoomTimeline key={room.id} room={room} models={models} /> : <p className="p-4">Create or select a room to start.</p>}
      </div>
    </main>
  )
}
