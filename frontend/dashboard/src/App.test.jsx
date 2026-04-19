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

  test('renders empty overview state with interactive settings', async () => {
    const fetchMock = vi.fn((input, init) => {
      const { pathname, searchParams } = parseRequest(input)

      if (pathname === '/api/books') {
        return jsonResponse({
          items: [
            {
              book_id: 'book_a',
              title: 'Book A',
              status: 'active',
              profile_completed: false,
              created_at: '2026-04-19T10:00:00+00:00',
              updated_at: '2026-04-19T10:00:00+00:00',
            },
          ],
        })
      }

      if (pathname === '/api/books/book_a/profile') {
        return jsonResponse({
          book_id: 'book_a',
          completed: false,
          background: '',
          worldview: '',
          era_setting: '',
          genre: '',
          protagonist: '',
          protagonist_goal: '',
          core_conflict: '',
          narrative_style: '',
          created_at: null,
          updated_at: null,
        })
      }

      if (pathname === '/api/books/book_a/interactive-settings') {
        return jsonResponse({
          book_id: 'book_a',
          uncertainty_enabled: false,
          decision_timeout_seconds: 60,
          created_at: '2026-04-19T10:00:00+00:00',
          updated_at: '2026-04-19T10:00:00+00:00',
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

      if (pathname.includes('/api/control/scenes/') && pathname.endsWith('/run')) {
        return jsonResponse({
          book_id: 'book_a',
          scene_id: 'n/a',
          status: 'idle',
          run_id: null,
          current_turn: 0,
          target_turns: 0,
          pending_decision: null,
          recent_interventions: [],
        })
      }

      if (pathname.includes('/api/control/scenes/') && pathname.endsWith('/decisions/pending')) {
        return jsonResponse({
          book_id: 'book_a',
          scene_id: 'n/a',
          item: null,
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
    expect(screen.getByText('不确定时询问用户')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新建书籍向导' })).toBeInTheDocument()

    await waitFor(() => {
      expect(window.location.pathname).toBe('/books/book_a/overview')
    })
  })

  test('starts async scene, interrupts and selects pending decision', async () => {
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
              profile_completed: true,
              created_at: '2026-04-19T10:00:00+00:00',
              updated_at: '2026-04-19T10:00:00+00:00',
            },
          ],
        })
      }

      if (req.pathname === '/api/books/book_a/profile') {
        return jsonResponse({
          book_id: 'book_a',
          completed: true,
          background: '港口旧城',
          worldview: '现代都市',
          era_setting: '近未来',
          genre: '悬疑',
          protagonist: '林湛',
          protagonist_goal: '查明真相',
          core_conflict: '家人与真相冲突',
          narrative_style: '冷峻',
          created_at: '2026-04-19T10:00:00+00:00',
          updated_at: '2026-04-19T10:00:00+00:00',
        })
      }

      if (req.pathname === '/api/books/book_a/interactive-settings') {
        return jsonResponse({
          book_id: 'book_a',
          uncertainty_enabled: true,
          decision_timeout_seconds: 60,
          created_at: '2026-04-19T10:00:00+00:00',
          updated_at: '2026-04-19T10:00:00+00:00',
        })
      }

      if (req.pathname === '/api/dashboard/kpis') {
        return jsonResponse({
          book_id: 'book_a',
          total_scenes: 1,
          completed_scenes: 0,
          completion_rate: 0,
          total_turns: 0,
          active_agents: 0,
          total_cost: 0,
        })
      }

      if (req.pathname === '/api/dashboard/scenes') {
        return jsonResponse({
          book_id: 'book_a',
          items: [
            {
              book_id: 'book_a',
              scene_id: 's-beta',
              status: 'running',
              total_turns: 1,
              active_agents: 1,
              last_actor: 'hero',
              last_action: '观察',
              last_updated: '2026-04-19T10:05:00+00:00',
            },
          ],
        })
      }

      if (req.pathname === '/api/dashboard/agents') {
        return jsonResponse({ book_id: 'book_a', items: [] })
      }

      if (req.pathname === '/api/dashboard/costs') {
        return jsonResponse({ book_id: 'book_a', scope: 'current', series: [], by_agent: [] })
      }

      if (req.pathname === '/api/dashboard/scenes/s-beta/turns') {
        return jsonResponse({
          book_id: 'book_a',
          scene_id: 's-beta',
          items: [
            {
              turn: 1,
              actor: 'hero',
              created_at: '2026-04-19T10:06:00+00:00',
              action: { action: '逼近', speech: '交出来', goal_progress: '推进' },
              decision: { conflict: null },
              state_delta: {},
            },
          ],
        })
      }

      if (req.pathname === '/api/control/scenes/s-beta/run') {
        return jsonResponse({
          book_id: 'book_a',
          scene_id: 's-beta',
          run_id: 'run-1',
          status: 'waiting_user',
          current_turn: 1,
          target_turns: 6,
          last_error: null,
          pending_decision: null,
          recent_interventions: [],
        })
      }

      if (req.pathname === '/api/control/scenes/s-beta/decisions/pending') {
        return jsonResponse({
          book_id: 'book_a',
          scene_id: 's-beta',
          item: {
            request_id: 'd-1',
            question: '请选择处理方式',
            recommended_option: 'accept_director',
            remaining_seconds: 30,
            options: [
              { id: 'accept_director', label: '按导演裁决推进' },
              { id: 'use_actor_proposal', label: '采用角色原提案' },
            ],
          },
        })
      }

      if (req.pathname === '/api/control/scenes/start_async' && method === 'POST') {
        return jsonResponse({
          book_id: 'book_a',
          scene_id: 's-beta',
          run_id: 'run-1',
          status: 'running',
          current_turn: 0,
          target_turns: 6,
        })
      }

      if (req.pathname === '/api/control/scenes/s-beta/interrupt' && method === 'POST') {
        return jsonResponse({
          book_id: 'book_a',
          scene_id: 's-beta',
          run_id: 'run-1',
          status: 'running',
          current_turn: 1,
          target_turns: 6,
        })
      }

      if (req.pathname === '/api/control/scenes/s-beta/decisions/d-1/select' && method === 'POST') {
        return jsonResponse({
          request_id: 'd-1',
          status: 'resolved',
          selected_option: 'accept_director',
        })
      }

      if (req.pathname === '/api/control/scenes/s-beta/pause' && method === 'POST') {
        return jsonResponse({ book_id: 'book_a', scene_id: 's-beta', status: 'paused' })
      }

      return jsonResponse({})
    })

    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await screen.findByText('场景列表')

    fireEvent.click(screen.getByRole('button', { name: /s-beta/ }))

    fireEvent.change(screen.getByLabelText('创作者打断想法'), {
      target: { value: '这一段主角应该更克制' },
    })
    fireEvent.click(screen.getByRole('button', { name: '打断并重算' }))

    await waitFor(() => {
      expect(calls.some((call) => call.pathname === '/api/control/scenes/s-beta/interrupt')).toBe(true)
    })

    fireEvent.click(screen.getByRole('button', { name: '按导演裁决推进' }))
    await waitFor(() => {
      expect(calls.some((call) => call.pathname === '/api/control/scenes/s-beta/decisions/d-1/select')).toBe(
        true,
      )
    })

    fireEvent.change(screen.getByLabelText('目标'), { target: { value: '推进剧情' } })
    fireEvent.change(screen.getByLabelText('参与角色（逗号分隔）'), {
      target: { value: 'hero' },
    })
    fireEvent.click(screen.getByRole('button', { name: '异步开始执行' }))
    await waitFor(() => {
      expect(calls.some((call) => call.pathname === '/api/control/scenes/start_async')).toBe(true)
    })
  })

  test('book wizard creates and edits profile', async () => {
    const calls = []
    const fetchMock = vi.fn((input, init) => {
      const req = parseRequest(input)
      const method = (init?.method || 'GET').toUpperCase()
      calls.push({ pathname: req.pathname, method, body: init?.body })

      if (req.pathname === '/api/books') {
        if (method === 'POST') {
          return jsonResponse({
            book_id: 'book_new',
            title: '新书',
            status: 'idle',
            profile_completed: true,
            created_at: '2026-04-19T10:00:00+00:00',
            updated_at: '2026-04-19T10:00:00+00:00',
          })
        }
        return jsonResponse({
          items: [
            {
              book_id: 'book_a',
              title: 'Book A',
              status: 'active',
              profile_completed: false,
              created_at: '2026-04-19T10:00:00+00:00',
              updated_at: '2026-04-19T10:00:00+00:00',
            },
            {
              book_id: 'book_new',
              title: '新书',
              status: 'idle',
              profile_completed: true,
              created_at: '2026-04-19T10:00:00+00:00',
              updated_at: '2026-04-19T10:00:00+00:00',
            },
          ],
        })
      }

      if (req.pathname === '/api/books/book_new/activate' && method === 'POST') {
        return jsonResponse({ ok: true })
      }

      if (req.pathname === '/api/books/book_a/profile') {
        return jsonResponse({
          book_id: 'book_a',
          completed: false,
          background: '',
          worldview: '',
          era_setting: '',
          genre: '',
          protagonist: '',
          protagonist_goal: '',
          core_conflict: '',
          narrative_style: '',
          created_at: null,
          updated_at: null,
        })
      }

      if (req.pathname === '/api/books/book_new/profile') {
        if (method === 'PATCH') {
          return jsonResponse({
            book_id: 'book_new',
            completed: true,
            background: '背景',
            worldview: '世界观',
            era_setting: '时代',
            genre: '题材',
            protagonist: '主角',
            protagonist_goal: '目标',
            core_conflict: '新冲突',
            narrative_style: '风格',
            created_at: '2026-04-19T10:00:00+00:00',
            updated_at: '2026-04-19T10:10:00+00:00',
          })
        }
        return jsonResponse({
          book_id: 'book_new',
          completed: true,
          background: '背景',
          worldview: '世界观',
          era_setting: '时代',
          genre: '题材',
          protagonist: '主角',
          protagonist_goal: '目标',
          core_conflict: '冲突',
          narrative_style: '风格',
          created_at: '2026-04-19T10:00:00+00:00',
          updated_at: '2026-04-19T10:00:00+00:00',
        })
      }

      if (req.pathname.includes('/interactive-settings')) {
        return jsonResponse({
          book_id: 'book_a',
          uncertainty_enabled: false,
          decision_timeout_seconds: 60,
          created_at: '2026-04-19T10:00:00+00:00',
          updated_at: '2026-04-19T10:00:00+00:00',
        })
      }

      if (req.pathname === '/api/dashboard/kpis') {
        return jsonResponse({
          book_id: req.searchParams.get('book_id') || 'book_a',
          total_scenes: 0,
          completed_scenes: 0,
          completion_rate: 0,
          total_turns: 0,
          active_agents: 0,
          total_cost: 0,
        })
      }

      if (req.pathname === '/api/dashboard/scenes') {
        return jsonResponse({ book_id: req.searchParams.get('book_id') || 'book_a', items: [] })
      }
      if (req.pathname === '/api/dashboard/agents') {
        return jsonResponse({ book_id: req.searchParams.get('book_id') || 'book_a', items: [] })
      }
      if (req.pathname === '/api/dashboard/costs') {
        return jsonResponse({ book_id: req.searchParams.get('book_id') || 'book_a', scope: 'current', series: [], by_agent: [] })
      }
      if (req.pathname.includes('/api/control/scenes/') && req.pathname.endsWith('/run')) {
        return jsonResponse({
          book_id: req.searchParams.get('book_id') || 'book_a',
          scene_id: 'n/a',
          status: 'idle',
          run_id: null,
          current_turn: 0,
          target_turns: 0,
          pending_decision: null,
          recent_interventions: [],
        })
      }
      if (req.pathname.includes('/api/control/scenes/') && req.pathname.endsWith('/decisions/pending')) {
        return jsonResponse({ book_id: req.searchParams.get('book_id') || 'book_a', scene_id: 'n/a', item: null })
      }
      return jsonResponse({})
    })

    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    await screen.findByText('当前书籍设定')

    fireEvent.click(screen.getByRole('button', { name: '新建书籍向导' }))
    fireEvent.change(screen.getByLabelText('book_id'), { target: { value: 'book_new' } })
    fireEvent.change(screen.getByLabelText('书籍标题'), { target: { value: '新书' } })
    fireEvent.change(screen.getByLabelText('背景'), { target: { value: '背景' } })
    fireEvent.change(screen.getByLabelText('世界观'), { target: { value: '世界观' } })
    fireEvent.change(screen.getByLabelText('时代设定'), { target: { value: '时代' } })
    fireEvent.change(screen.getByLabelText('题材类型'), { target: { value: '题材' } })
    fireEvent.change(screen.getByLabelText('主角'), { target: { value: '主角' } })
    fireEvent.change(screen.getByLabelText('主角目标'), { target: { value: '目标' } })
    fireEvent.change(screen.getByLabelText('核心冲突'), { target: { value: '冲突' } })
    fireEvent.change(screen.getByLabelText('叙事风格'), { target: { value: '风格' } })
    fireEvent.click(screen.getByRole('button', { name: '创建并切换' }))

    await waitFor(() => {
      expect(calls.some((call) => call.pathname === '/api/books' && call.method === 'POST')).toBe(true)
    })

    await waitFor(() => {
      expect(window.location.pathname).toBe('/books/book_new/overview')
    })

    fireEvent.click(screen.getByRole('button', { name: '编辑设定' }))
    fireEvent.change(screen.getByLabelText('核心冲突'), { target: { value: '新冲突' } })
    fireEvent.click(screen.getByRole('button', { name: '保存设定' }))

    await waitFor(() => {
      expect(calls.some((call) => call.pathname === '/api/books/book_new/profile' && call.method === 'PATCH')).toBe(
        true,
      )
    })
  })
})
