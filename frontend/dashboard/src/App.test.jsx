import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import App from './App'

function jsonResponse(body, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
}

function parseRequest(input) {
  const raw = String(input)
  const url = new URL(raw, 'http://localhost')
  return {
    pathname: url.pathname,
    searchParams: url.searchParams,
  }
}

describe('Dashboard App', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    window.history.pushState({}, '', '/')
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  test('renders empty overview state with default book routing', async () => {
    const fetchMock = vi.fn((input, init) => {
      const { pathname, searchParams } = parseRequest(input)

      if (pathname === '/api/books') {
        return jsonResponse({
          items: [
            {
              book_id: 'book_a',
              title: 'Book A',
              status: 'active',
              created_at: '2026-04-19T10:00:00+00:00',
              updated_at: '2026-04-19T10:00:00+00:00',
            },
          ],
        })
      }

      if (pathname === '/api/dashboard/kpis') {
        const bookId = searchParams.get('book_id') || 'default_book'
        return jsonResponse({
          book_id: bookId,
          total_scenes: 0,
          completed_scenes: 0,
          completion_rate: 0,
          total_turns: 0,
          active_agents: 0,
          total_cost: 0,
        })
      }

      if (pathname === '/api/dashboard/scenes') {
        return jsonResponse({ book_id: searchParams.get('book_id') || 'default_book', items: [] })
      }

      if (pathname === '/api/dashboard/agents') {
        return jsonResponse({ book_id: searchParams.get('book_id') || 'default_book', items: [] })
      }

      if (pathname === '/api/dashboard/costs') {
        return jsonResponse({
          book_id: searchParams.get('book_id') || 'default_book',
          scope: searchParams.get('scope') || 'current',
          series: [],
          by_agent: [],
        })
      }

      if ((init?.method || 'GET').toUpperCase() === 'POST') {
        return jsonResponse({ ok: true })
      }

      return jsonResponse({ scene_id: 'n/a', items: [] })
    })

    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(screen.getByText('数据同步中...')).toBeInTheDocument()
    await screen.findByText('暂无场景数据，先在“场景进度”页启动一次场景。')
    expect(screen.getByText('0.0%')).toBeInTheDocument()
    expect(screen.getByLabelText('当前书籍')).toBeInTheDocument()

    await waitFor(() => {
      expect(window.location.pathname).toBe('/books/book_a/overview')
    })
  })

  test('filters scenes, pauses selected scene, and switches book', async () => {
    window.history.pushState({}, '', '/books/book_a/scenes')

    const calls = []
    const fetchMock = vi.fn((input, init) => {
      const req = parseRequest(input)
      const method = (init?.method || 'GET').toUpperCase()
      calls.push({ pathname: req.pathname, search: req.searchParams.toString(), method, body: init?.body })

      if (req.pathname === '/api/books') {
        return jsonResponse({
          items: [
            {
              book_id: 'book_a',
              title: 'Book A',
              status: 'active',
              created_at: '2026-04-19T10:00:00+00:00',
              updated_at: '2026-04-19T10:00:00+00:00',
            },
            {
              book_id: 'book_b',
              title: 'Book B',
              status: 'idle',
              created_at: '2026-04-19T10:00:00+00:00',
              updated_at: '2026-04-19T10:00:00+00:00',
            },
          ],
        })
      }

      if (req.pathname === '/api/dashboard/kpis') {
        return jsonResponse({
          book_id: req.searchParams.get('book_id') || 'book_a',
          total_scenes: 2,
          completed_scenes: 1,
          completion_rate: 0.5,
          total_turns: 5,
          active_agents: 2,
          total_cost: 0.013,
        })
      }

      if (req.pathname === '/api/dashboard/scenes') {
        const bookId = req.searchParams.get('book_id') || 'book_a'
        if (bookId === 'book_b') {
          return jsonResponse({
            book_id: 'book_b',
            items: [
              {
                book_id: 'book_b',
                scene_id: 's-gamma',
                status: 'ready',
                total_turns: 1,
                active_agents: 1,
                last_actor: 'hero',
                last_action: '观察',
                last_updated: '2026-04-19T10:10:00+00:00',
              },
            ],
          })
        }
        return jsonResponse({
          book_id: 'book_a',
          items: [
            {
              book_id: 'book_a',
              scene_id: 's-alpha',
              status: 'ready',
              total_turns: 2,
              active_agents: 1,
              last_actor: 'hero',
              last_action: '观察',
              last_updated: '2026-04-19T10:00:00+00:00',
            },
            {
              book_id: 'book_a',
              scene_id: 's-beta',
              status: 'running',
              total_turns: 3,
              active_agents: 2,
              last_actor: 'villain',
              last_action: '逼近',
              last_updated: '2026-04-19T10:05:00+00:00',
            },
          ],
        })
      }

      if (req.pathname === '/api/dashboard/agents') {
        return jsonResponse({ book_id: req.searchParams.get('book_id') || 'book_a', items: [] })
      }

      if (req.pathname === '/api/dashboard/costs') {
        return jsonResponse({
          book_id: req.searchParams.get('book_id') || 'book_a',
          scope: req.searchParams.get('scope') || 'current',
          series: [],
          by_agent: [],
        })
      }

      if (req.pathname === '/api/dashboard/scenes/s-alpha/turns') {
        return jsonResponse({
          book_id: req.searchParams.get('book_id') || 'book_a',
          scene_id: 's-alpha',
          items: [
            {
              turn: 1,
              actor: 'hero',
              created_at: '2026-04-19T10:00:00+00:00',
              action: { action: '观察', speech: '...' },
              decision: { conflict: null },
              state_delta: {},
            },
          ],
        })
      }

      if (req.pathname === '/api/dashboard/scenes/s-beta/turns') {
        return jsonResponse({
          book_id: req.searchParams.get('book_id') || 'book_a',
          scene_id: 's-beta',
          items: [
            {
              turn: 1,
              actor: 'villain',
              created_at: '2026-04-19T10:06:00+00:00',
              action: { action: '逼近', speech: '交出来' },
              decision: { conflict: null },
              state_delta: {},
            },
          ],
        })
      }

      if (req.pathname === '/api/control/scenes/s-beta/pause' && method === 'POST') {
        return jsonResponse({ book_id: 'book_a', scene_id: 's-beta', status: 'paused' })
      }

      if (req.pathname === '/api/books/book_b/activate' && method === 'POST') {
        return jsonResponse({
          book_id: 'book_b',
          title: 'Book B',
          status: 'active',
          created_at: '2026-04-19T10:00:00+00:00',
          updated_at: '2026-04-19T10:15:00+00:00',
        })
      }

      if (method === 'POST') {
        return jsonResponse({ ok: true })
      }

      return jsonResponse({})
    })

    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await screen.findByText('场景列表')

    const filterInput = screen.getByLabelText('场景筛选')
    fireEvent.change(filterInput, { target: { value: 'beta' } })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /s-beta/ })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /s-alpha/ })).not.toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: /s-beta/ }))
    await waitFor(() => {
      expect(
        calls.some(
          (call) =>
            call.pathname === '/api/dashboard/scenes/s-beta/turns' && call.search.includes('book_id=book_a'),
        ),
      ).toBe(true)
    })

    fireEvent.click(screen.getByRole('button', { name: '暂停' }))
    await waitFor(() => {
      const pauseCall = calls.find((call) => call.pathname === '/api/control/scenes/s-beta/pause')
      expect(pauseCall).toBeTruthy()
      expect(JSON.parse(pauseCall.body)).toMatchObject({ book_id: 'book_a' })
    })

    fireEvent.change(screen.getByLabelText('当前书籍'), { target: { value: 'book_b' } })
    await waitFor(() => {
      expect(
        calls.some((call) => call.pathname === '/api/books/book_b/activate' && call.method === 'POST'),
      ).toBe(true)
    })

    await waitFor(() => {
      expect(
        calls.some(
          (call) => call.pathname === '/api/dashboard/scenes' && call.search.includes('book_id=book_b'),
        ),
      ).toBe(true)
    })
  })
})
