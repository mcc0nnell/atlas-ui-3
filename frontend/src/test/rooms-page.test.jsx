import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import RoomsPage from '../components/RoomsPage'

vi.mock('../contexts/ChatContext', () => ({ useChat: () => ({ models: [{ name: 'model-a' }] }) }))

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data })
const page = events => ({ events, next_after: events.at(-1)?.sequence || 0, has_more: false })
const event = (sequence, type, content) => ({ sequence, type, content, sender: 'owner@example.com' })

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('RoomsPage', () => {
  it('creates a room and sends a message to its durable timeline', async () => {
    const messages = []
    const fetcher = vi.fn(async (url, options) => {
      if (url === '/api/rooms' && !options) return response({ rooms: [] })
      if (url === '/api/rooms') return response({ id: 'r1', name: 'Atlas' }, 201)
      if (options?.method === 'POST') {
        const payload = JSON.parse(options.body)
        messages.push(event(1, payload.event.type, { text: payload.event.text }))
        return response(messages[0])
      }
      return response(page(messages))
    })
    vi.stubGlobal('fetch', fetcher)
    render(<MemoryRouter><RoomsPage /></MemoryRouter>)
    fireEvent.change(screen.getByLabelText('Room name'), { target: { value: 'Atlas' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
    await screen.findByRole('heading', { name: 'Atlas' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh timeline' })).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Hello team' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    expect(await screen.findByText('Hello team')).toBeInTheDocument()
    expect(screen.getByLabelText('Message')).toHaveValue('')
  })

  it('isolates timelines when switching rooms and renders roster changes', async () => {
    vi.stubGlobal('fetch', vi.fn(async url => {
      if (url === '/api/rooms') return response({ rooms: [{ id: 'a', name: 'Atlas' }, { id: 'b', name: 'Thread' }] })
      if (url.includes('/a/')) return response(page([
        event(1, 'room.message', { text: 'Atlas only' }),
        event(2, 'room.agent.added', { agent_id: 'Reviewer' }),
      ]))
      return response(page([event(1, 'room.message', { text: 'Thread only' })]))
    }))
    render(<MemoryRouter><RoomsPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Atlas' }))
    expect(await screen.findByText('Atlas only')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove Reviewer' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Thread' }))
    expect(await screen.findByText('Thread only')).toBeInTheDocument()
    expect(screen.queryByText('Atlas only')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove Reviewer' })).not.toBeInTheDocument()
  })

  it('retries an unconfirmed append with the same transaction identifier', async () => {
    const attempts = []
    const fetcher = vi.fn(async (url, options) => {
      if (url === '/api/rooms') return response({ rooms: [{ id: 'a', name: 'Atlas' }] })
      if (options?.method === 'POST') {
        attempts.push(JSON.parse(options.body))
        if (attempts.length === 1) throw new Error('Lost reply')
        return response(event(1, 'room.message', { text: 'Retry me' }))
      }
      return response(page(attempts.length > 1 ? [event(1, 'room.message', { text: 'Retry me' })] : []))
    })
    vi.stubGlobal('fetch', fetcher)
    render(<MemoryRouter><RoomsPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Atlas' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh timeline' })).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Retry me' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry pending event' }))
    expect(await screen.findByText('Retry me')).toBeInTheDocument()
    expect(attempts).toHaveLength(2)
    expect(attempts[0]).toEqual(attempts[1])
  })
})

it('requests one model-backed roster response and renders its attribution', async () => {
  const turns = []
  const roster = event(1, 'room.agent.added', { agent_id: 'Reviewer' })
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (url === '/api/rooms') return response({ rooms: [{ id: 'a', name: 'Atlas' }] })
    if (url.endsWith('/turns')) {
      turns.push(JSON.parse(options.body))
      return response({ state: 'completed' })
    }
    if (url.includes('after=1')) return response(page([
      event(2, 'room.agent.reply', { agent_id: 'Reviewer', model: 'model-a', text: 'Design reviewed' }),
    ]))
    return response(page([roster]))
  }))
  render(<MemoryRouter><RoomsPage /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Atlas' }))
  await screen.findByRole('button', { name: 'Remove Reviewer' })
  fireEvent.change(screen.getByLabelText('Participant'), { target: { value: 'Reviewer' } })
  fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'model-a' } })
  fireEvent.change(screen.getByLabelText('Ask agent'), { target: { value: 'Review the design' } })
  fireEvent.click(screen.getByRole('button', { name: 'Request response' }))
  expect(await screen.findByText('Design reviewed')).toBeInTheDocument()
  expect(screen.getByText('Reviewer · model-a · #2')).toBeInTheDocument()
  expect(turns).toHaveLength(1)
  expect(turns[0]).toMatchObject({ agent_id: 'Reviewer', model: 'model-a', prompt: 'Review the design' })
})

it('restores the participant model preference and remembered turn count', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => {
    if (url === '/api/rooms') return response({ rooms: [{ id: 'a', name: 'Atlas' }] })
    if (url.endsWith('/participants')) return response({ participants: [
      { agent_id: 'Reviewer', participant_id: 'stable-id', context_turns: 3, last_model: 'model-a' },
    ] })
    return response(page([event(1, 'room.agent.added', { agent_id: 'Reviewer' })]))
  }))
  render(<MemoryRouter><RoomsPage /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Atlas' }))
  expect(await screen.findByText('3 remembered turns')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Participant'), { target: { value: 'Reviewer' } })
  expect(screen.getByLabelText('Model')).toHaveValue('model-a')
})

it('saves and opens an inert artifact revision without treating it as a roster change', async () => {
  let saved
  const fetcher = vi.fn(async (url, options) => {
    if (url === '/api/rooms') return response({ rooms: [{ id: 'a', name: 'Atlas' }] })
    if (url.endsWith('/participants')) return response({ participants: [] })
    if (url.includes('/artifacts/')) return response({ ...saved.content, text: 'INPUT FROM echo' })
    if (options?.method === 'POST') {
      const payload = JSON.parse(options.body)
      expect(payload.event).toMatchObject({ kind: 'workflow', base_revision: 0, title: 'Pipeline', text: 'INPUT FROM echo' })
      saved = event(1, 'room.artifact.revised', { artifact_id: payload.event.artifact_id, kind: 'workflow', title: 'Pipeline', revision: 1 })
      return response(saved)
    }
    return response(page(saved ? [saved] : []))
  })
  vi.stubGlobal('fetch', fetcher)
  render(<MemoryRouter><RoomsPage /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Atlas' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh timeline' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('Artifact title'), { target: { value: 'Pipeline' } })
  fireEvent.change(screen.getByLabelText('Artifact type'), { target: { value: 'workflow' } })
  fireEvent.change(screen.getByLabelText('Artifact content'), { target: { value: 'INPUT FROM echo' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save artifact revision' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Open Pipeline revision 1' }))
  await waitFor(() => expect(screen.getByLabelText('Artifact content')).toHaveValue('INPUT FROM echo'))
  expect(screen.getByText('Editing revision 1. Saving creates a new revision.')).toBeInTheDocument()
  expect(screen.queryByText('Removed roster entry: undefined')).not.toBeInTheDocument()
})


it('creates a standard notebook, edits cells, and posts a revision-linked discussion', async () => {
  const events = []
  const revisions = new Map()
  const posts = []
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    if (url === '/api/rooms') return response({ rooms: [{ id: 'a', name: 'Atlas' }] })
    if (url.endsWith('/participants')) return response({ participants: [] })
    if (url.includes('/artifacts/')) return response(revisions.get(Number(url.split('/').at(-1))))
    if (options?.method === 'POST') {
      const payload = JSON.parse(options.body).event
      posts.push(payload)
      if (payload.type === 'room.artifact.revised') {
        const content = { artifact_id: payload.artifact_id, kind: payload.kind, title: payload.title, revision: payload.base_revision + 1 }
        revisions.set(content.revision, { ...content, text: payload.text })
        events.push(event(events.length + 1, payload.type, content))
      } else events.push(event(events.length + 1, payload.type, payload))
      return response(events.at(-1))
    }
    const after = Number(new URL(url, 'http://localhost').searchParams.get('after'))
    return response({ events: events.filter(e => e.sequence > after), next_after: events.at(-1)?.sequence || after, has_more: false })
  }))
  render(<MemoryRouter><RoomsPage /></MemoryRouter>)
  fireEvent.click(await screen.findByRole('button', { name: 'Atlas' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh timeline' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('Artifact title'), { target: { value: 'Research' } })
  fireEvent.change(screen.getByLabelText('Artifact type'), { target: { value: 'notebook' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add code cell' }))
  fireEvent.change(screen.getByLabelText('Cell 1 · code'), { target: { value: '10 + 20' } })
  expect(screen.getByRole('button', { name: 'Discuss cell 1' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Save artifact revision' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Open Research revision 1' }))
  await waitFor(() => expect(screen.getByLabelText('Cell 1 · code')).toHaveValue('10 + 20'))
  const notebook = JSON.parse(posts[0].text)
  expect(notebook).toMatchObject({ nbformat: 4, nbformat_minor: 5, cells: [{ cell_type: 'code', source: '10 + 20', outputs: [], execution_count: null }] })
  expect(screen.getByRole('link', { name: 'Download saved notebook' })).toHaveAttribute('download', 'notebook.ipynb')
  fireEvent.click(screen.getByRole('button', { name: 'Discuss cell 1' }))
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Check this' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  expect(await screen.findByText('Check this')).toBeInTheDocument()
  expect(posts[1]).toMatchObject({ type: 'room.notebook.comment', artifact_id: posts[0].artifact_id, revision: 1, cell_id: notebook.cells[0].id, text: 'Check this' })
  const second = { ...revisions.get(1), revision: 2, text: JSON.stringify({ ...notebook, cells: [{ ...notebook.cells[0], source: '10 + 20 + 1' }] }) }
  revisions.set(2, second)
  events.push(event(3, 'room.artifact.revised', { artifact_id: second.artifact_id, revision: 2, title: 'Research', kind: 'notebook' }))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh timeline' }))
  expect(await screen.findByText('Revision 2 is available. Your current draft is unchanged.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Open latest revision' }))
  await waitFor(() => expect(screen.getByLabelText('Cell 1 · code')).toHaveValue('10 + 20 + 1'))
})
