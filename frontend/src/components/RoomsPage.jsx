import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

const button = 'px-3 py-2 rounded border border-gray-600 hover:bg-gray-700 disabled:opacity-50'
const input = 'w-full min-w-0 rounded bg-gray-800 border border-gray-600 p-2 text-gray-100'

async function request(path, options) {
  const response = await fetch(path, options)
  if (!response.ok) throw new Error(`Room request failed (${response.status})`)
  return response.json()
}

function post(path, payload) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

function RoomTimeline({ room }) {
  const path = `/api/rooms/${encodeURIComponent(room.id)}/events`
  const [events, setEvents] = useState([])
  const cursor = useRef(0)
  const loading = useRef(false)
  const sending = useRef(false)
  const [busy, setBusy] = useState(false)
  const [more, setMore] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [agent, setAgent] = useState('')
  const [pending, setPending] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  const sync = useCallback(async () => {
    if (loading.current) return
    loading.current = true
    setBusy(true)
    try {
      const page = await request(`${path}?after=${cursor.current}&limit=100`)
      cursor.current = page.next_after
      setEvents(previous => [...previous, ...page.events])
      setMore(page.has_more)
    } catch (err) {
      setError(err.message)
    } finally {
      loading.current = false
      setBusy(false)
    }
  }, [path])

  useEffect(() => { sync() }, [sync])

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
      if (payload.event.type === 'room.message') setMessage('')
      if (payload.event.type === 'room.agent.added') setAgent('')
      await sync()
    } catch (err) {
      setError(err.message)
    } finally {
      sending.current = false
      setSubmitting(false)
    }
  }

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
                <div className="text-xs text-gray-400">{event.sender} · #{event.sequence}</div>
                <p className="whitespace-pre-wrap">{event.type === 'room.message'
                  ? event.content.text
                  : `${event.type === 'room.agent.added' ? 'Added' : 'Removed'} roster entry: ${event.content.agent_id}`}</p>
              </li>
            ))}
          </ol>
          {!events.length && !busy && <p className="text-gray-400">No messages yet.</p>}
          <form className="mt-4 space-y-2" onSubmit={e => { e.preventDefault(); append({ type: 'room.message', text: message.trim() }) }}>
            <label htmlFor="room-message">Message</label>
            <textarea id="room-message" className={input} rows={3} value={message}
              maxLength={16000} disabled={!!pending || submitting} onChange={e => setMessage(e.target.value)} />
            <button className={button} disabled={!message.trim() || !!pending || submitting || busy}>Send message</button>
          </form>
        </div>
        <aside className="border border-gray-700 rounded p-3 space-y-3">
          <h3 className="font-semibold">Agent roster</h3>
          <p className="text-xs text-gray-400">Roster entries only. Agents do not run or respond yet.{more && ' Load remaining events for the complete roster.'}</p>
          <ul className="space-y-2">
            {[...roster].sort().map(id => <li key={id} className="flex flex-wrap items-center justify-between gap-2">
              <span className="break-all">{id}</span>
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
        {room ? <RoomTimeline key={room.id} room={room} /> : <p className="p-4">Create or select a room to start.</p>}
      </div>
    </main>
  )
}
