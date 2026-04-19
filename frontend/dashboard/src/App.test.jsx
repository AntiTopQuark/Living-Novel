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

describe('Dashboard App', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  test('renders loading then empty overview state', async () => {
    const fetchMock = vi.fn((input, init) => {
      const url = String(input)
      if (url.endsWith('/api/dashboard/kpis')) {
        return jsonResponse({
          total_scenes: 0,
          completed_scenes: 0,
          completion_rate: 0,
          total_turns: 0,
          active_agents: 0,
          total_cost: 0,
        })
      }
      if (url.endsWith('/api/dashboard/scenes')) {
        return jsonResponse({ items: [] })
      }
      if (url.endsWith('/api/dashboard/agents')) {
        return jsonResponse({ items: [] })
      }
      if (url.endsWith('/api/dashboard/costs')) {
        return jsonResponse({ series: [], by_agent: [] })
      }
      if (init?.method === 'POST') {
        return jsonResponse({ ok: true })
      }
      return jsonResponse({ scene_id: 'n/a', items: [] })
    })

    vi.stubGlobal('fetch', fetchMock)

    render(<App />)

    expect(screen.getByText('数据同步中...')).toBeInTheDocument()
    await screen.findByText('暂无场景数据，先在“场景进度”页启动一次场景。')
    expect(screen.getByText('0.0%')).toBeInTheDocument()
  })

  test('filters scenes and triggers pause control', async () => {
    const calls = []
    const fetchMock = vi.fn((input, init) => {
      const url = String(input)
      const method = (init?.method || 'GET').toUpperCase()
      calls.push({ url, method })

      if (url.endsWith('/api/dashboard/kpis')) {
        return jsonResponse({
          total_scenes: 2,
          completed_scenes: 1,
          completion_rate: 0.5,
          total_turns: 5,
          active_agents: 2,
          total_cost: 0.013,
        })
      }
      if (url.endsWith('/api/dashboard/scenes')) {
        return jsonResponse({
          items: [
            {
              scene_id: 's-alpha',
              status: 'ready',
              total_turns: 2,
              active_agents: 1,
              last_actor: 'hero',
              last_action: '观察',
              last_updated: '2026-04-19T10:00:00+00:00',
            },
            {
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
      if (url.endsWith('/api/dashboard/agents')) {
        return jsonResponse({ items: [] })
      }
      if (url.endsWith('/api/dashboard/costs')) {
        return jsonResponse({ series: [], by_agent: [] })
      }
      if (url.endsWith('/api/dashboard/scenes/s-alpha/turns')) {
        return jsonResponse({
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
      if (url.endsWith('/api/dashboard/scenes/s-beta/turns')) {
        return jsonResponse({
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
      if (url.endsWith('/api/control/scenes/s-beta/pause') && method === 'POST') {
        return jsonResponse({ scene_id: 's-beta', status: 'paused' })
      }
      if (url.endsWith('/api/control/scenes/s-beta/resume') && method === 'POST') {
        return jsonResponse({ scene_id: 's-beta', status: 'ready' })
      }

      return jsonResponse({})
    })

    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await screen.findByText('最近场景进度')

    fireEvent.click(screen.getByRole('button', { name: '场景进度' }))
    await screen.findByText('场景列表')

    const filterInput = screen.getByLabelText('场景筛选')
    fireEvent.change(filterInput, { target: { value: 'beta' } })

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /s-beta/ })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /s-alpha/ })).not.toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: /s-beta/ }))
    await waitFor(() => {
      expect(calls.some((call) => call.url.endsWith('/api/dashboard/scenes/s-beta/turns'))).toBe(true)
    })

    fireEvent.click(screen.getByRole('button', { name: '暂停' }))
    await waitFor(() => {
      expect(calls.some((call) => call.url.endsWith('/api/control/scenes/s-beta/pause'))).toBe(true)
    })
  })
})
