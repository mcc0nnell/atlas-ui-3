import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import RoomsPage from '../components/RoomsPage'

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
