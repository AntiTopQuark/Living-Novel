import React, { useEffect, useMemo, useState } from 'react'

import { dashboardApi } from './api'
import CostTrendChart from './components/CostTrendChart'
import { usePollingQuery } from './hooks/usePollingQuery'

const TABS = [
  { id: 'overview', label: '流程总览' },
  { id: 'scenes', label: '场景进度' },
  { id: 'agents', label: '角色进度' },
]

const initialStartForm = {
  scene_id: '',
  title: '',
  objective: '',
  participants: '',
  context: '',
  state: '{"objective_achieved": false, "unresolved_conflicts": []}',
  max_turns: 6,
}

function formatPct(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`
}

function formatMoney(value) {
  return Number(value || 0).toFixed(4)
}

function formatLocalTime(value) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export default function App() {
  const [tab, setTab] = useState('overview')
  const [selectedSceneId, setSelectedSceneId] = useState('')
  const [sceneFilter, setSceneFilter] = useState('')
  const [startForm, setStartForm] = useState(initialStartForm)
  const [controlMessage, setControlMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [controlBusy, setControlBusy] = useState(false)

  const kpisQuery = usePollingQuery(() => dashboardApi.getKpis(), [], 5000)
  const scenesQuery = usePollingQuery(() => dashboardApi.getScenes(), [], 5000)
  const agentsQuery = usePollingQuery(() => dashboardApi.getAgents(), [], 5000)
  const costsQuery = usePollingQuery(() => dashboardApi.getCosts(), [], 5000)
  const turnsQuery = usePollingQuery(
    selectedSceneId ? () => dashboardApi.getSceneTurns(selectedSceneId) : null,
    [selectedSceneId],
    5000,
  )

  const scenes = scenesQuery.data?.items ?? []
  const agents = agentsQuery.data?.items ?? []
  const costs = costsQuery.data ?? { series: [], by_agent: [] }
  const turns = turnsQuery.data?.items ?? []

  const filteredScenes = useMemo(() => {
    const keyword = sceneFilter.trim().toLowerCase()
    if (!keyword) return scenes
    return scenes.filter((scene) => {
      const idMatch = String(scene.scene_id || '').toLowerCase().includes(keyword)
      const statusMatch = String(scene.status || '').toLowerCase().includes(keyword)
      const actorMatch = String(scene.last_actor || '').toLowerCase().includes(keyword)
      return idMatch || statusMatch || actorMatch
    })
  }, [sceneFilter, scenes])

  useEffect(() => {
    if (!selectedSceneId && scenes.length > 0) {
      setSelectedSceneId(scenes[0].scene_id)
    }
  }, [selectedSceneId, scenes])

  useEffect(() => {
    if (selectedSceneId) return
    if (!startForm.scene_id && scenes.length > 0) {
      const seed = scenes[0]
      setStartForm((prev) => ({
        ...prev,
        scene_id: seed.scene_id || prev.scene_id,
        title: seed.scene_id || prev.title,
      }))
    }
  }, [selectedSceneId, startForm.scene_id, scenes])

  const anyLoading =
    kpisQuery.loading || scenesQuery.loading || agentsQuery.loading || costsQuery.loading

  const anyError =
    kpisQuery.error || scenesQuery.error || agentsQuery.error || costsQuery.error || turnsQuery.error

  async function refreshAll() {
    await Promise.all([
      kpisQuery.refresh(),
      scenesQuery.refresh(),
      agentsQuery.refresh(),
      costsQuery.refresh(),
      turnsQuery.refresh(),
    ])
  }

  async function handleStart(event) {
    event.preventDefault()
    setControlBusy(true)
    setNotice('')

    try {
      let parsedState = {}
      try {
        parsedState = JSON.parse(startForm.state || '{}')
      } catch {
        throw new Error('状态 JSON 解析失败，请检查 state 字段')
      }

      const participants = startForm.participants
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean)

      if (participants.length === 0) {
        throw new Error('participants 不能为空，使用逗号分隔角色 agent_id')
      }

      const response = await dashboardApi.startScene({
        scene_id: startForm.scene_id.trim(),
        title: startForm.title.trim(),
        objective: startForm.objective.trim(),
        participants,
        context: startForm.context,
        state: parsedState,
        max_turns: Number(startForm.max_turns) || undefined,
      })

      setNotice(`场景 ${response.scene_id} 已执行，状态 ${response.status}，回合 ${response.turns}`)
      setSelectedSceneId(response.scene_id)
      await refreshAll()
    } catch (error) {
      setNotice(error.message)
    } finally {
      setControlBusy(false)
    }
  }

  async function handlePause() {
    if (!selectedSceneId) {
      setNotice('请先选择场景再暂停')
      return
    }
    setControlBusy(true)
    setNotice('')
    try {
      const result = await dashboardApi.pauseScene(selectedSceneId, controlMessage)
      setNotice(`场景 ${result.scene_id} 已暂停`)
      await refreshAll()
    } catch (error) {
      setNotice(error.message)
    } finally {
      setControlBusy(false)
    }
  }

  async function handleResume() {
    if (!selectedSceneId) {
      setNotice('请先选择场景再继续')
      return
    }
    setControlBusy(true)
    setNotice('')
    try {
      const result = await dashboardApi.resumeScene(selectedSceneId, controlMessage)
      setNotice(`场景 ${result.scene_id} 已恢复到可执行状态`)
      await refreshAll()
    } catch (error) {
      setNotice(error.message)
    } finally {
      setControlBusy(false)
    }
  }

  const kpis = kpisQuery.data ?? {
    total_scenes: 0,
    completed_scenes: 0,
    completion_rate: 0,
    total_turns: 0,
    active_agents: 0,
    total_cost: 0,
  }

  return (
    <div className="dashboard-shell">
      <div className="bg-layer bg-layer-a" />
      <div className="bg-layer bg-layer-b" />

      <header className="topbar">
        <div>
          <p className="eyebrow">Living Novel</p>
          <h1>整理流程与进度看板</h1>
        </div>
        <div className="sync-state">
          <span className={anyLoading ? 'pulse-dot' : 'pulse-dot steady'} />
          <span>{anyLoading ? '数据同步中...' : '实时轮询每 5 秒'}</span>
        </div>
      </header>

      <nav className="tabbar">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={tab === item.id ? 'tab active' : 'tab'}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {anyError ? <div className="error-banner">{anyError.message}</div> : null}
      {notice ? <div className="notice-banner">{notice}</div> : null}

      {tab === 'overview' ? (
        <section className="panel-grid">
          <div className="kpi-grid">
            <KpiCard title="场景完成率" value={formatPct(kpis.completion_rate)} hint={`${kpis.completed_scenes}/${kpis.total_scenes}`} />
            <KpiCard title="总回合数" value={kpis.total_turns} hint="累计 scene_turn_logs" />
            <KpiCard title="活跃角色" value={kpis.active_agents} hint="最近有行动的角色" />
            <KpiCard title="累计成本" value={`$${formatMoney(kpis.total_cost)}`} hint="usage_events 总成本" />
          </div>

          <article className="card span-two">
            <header className="card-head">
              <h2>最近场景进度</h2>
              <span className="muted">按最近更新时间排序</span>
            </header>
            {scenes.length === 0 ? (
              <p className="muted">暂无场景数据，先在“场景进度”页启动一次场景。</p>
            ) : (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Scene</th>
                    <th>状态</th>
                    <th>回合</th>
                    <th>角色</th>
                    <th>最后动作</th>
                    <th>更新时间</th>
                  </tr>
                </thead>
                <tbody>
                  {scenes.slice(0, 8).map((item) => (
                    <tr key={item.scene_id}>
                      <td>{item.scene_id}</td>
                      <td>
                        <StatusPill status={item.status} />
                      </td>
                      <td>{item.total_turns}</td>
                      <td>{item.active_agents}</td>
                      <td>{item.last_action || '-'}</td>
                      <td>{formatLocalTime(item.last_updated)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </article>

          <article className="card span-two">
            <header className="card-head">
              <h2>Token 成本趋势</h2>
              <span className="muted">按天聚合</span>
            </header>
            <CostTrendChart series={costs.series} />
            <div className="cost-agent-grid">
              {(costs.by_agent || []).length === 0 ? (
                <p className="muted">暂无成本数据。</p>
              ) : (
                (costs.by_agent || []).map((item) => (
                  <div key={item.agent_id} className="mini-card">
                    <h4>{item.agent_id}</h4>
                    <p>请求 {item.requests}</p>
                    <p>Token {item.total_tokens}</p>
                    <p>成本 ${formatMoney(item.total_cost)}</p>
                  </div>
                ))
              )}
            </div>
          </article>
        </section>
      ) : null}

      {tab === 'scenes' ? (
        <section className="scene-layout">
          <article className="card scene-list">
            <header className="card-head">
              <h2>场景列表</h2>
              <span className="muted">筛选 scene_id / 状态 / 角色</span>
            </header>
            <input
              value={sceneFilter}
              onChange={(event) => setSceneFilter(event.target.value)}
              className="text-input"
              placeholder="输入关键词筛选"
              aria-label="场景筛选"
            />
            {filteredScenes.length === 0 ? (
              <p className="muted">没有匹配场景。</p>
            ) : (
              <ul className="scene-items">
                {filteredScenes.map((item) => (
                  <li key={item.scene_id}>
                    <button
                      type="button"
                      className={selectedSceneId === item.scene_id ? 'scene-btn active' : 'scene-btn'}
                      onClick={() => setSelectedSceneId(item.scene_id)}
                    >
                      <div className="scene-btn-main">
                        <strong>{item.scene_id}</strong>
                        <StatusPill status={item.status} />
                      </div>
                      <div className="scene-btn-sub">
                        <span>回合 {item.total_turns}</span>
                        <span>角色 {item.active_agents}</span>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </article>

          <article className="card scene-timeline">
            <header className="card-head">
              <h2>回合时间轴</h2>
              <span className="muted">{selectedSceneId || '未选择场景'}</span>
            </header>
            {!selectedSceneId ? (
              <p className="muted">请选择左侧场景查看回合细节。</p>
            ) : turns.length === 0 ? (
              <p className="muted">该场景暂无回合数据。</p>
            ) : (
              <ol className="turn-list">
                {turns.map((item) => (
                  <li key={`${item.turn}-${item.actor}`} className="turn-item">
                    <div className="turn-head">
                      <strong>Turn {item.turn}</strong>
                      <span>{item.actor}</span>
                      <span>{formatLocalTime(item.created_at)}</span>
                    </div>
                    <div className="turn-body">
                      <p>
                        <b>动作:</b> {item.action?.action || '-'}
                      </p>
                      <p>
                        <b>台词:</b> {item.action?.speech || '-'}
                      </p>
                      <p>
                        <b>目标推进:</b> {item.action?.goal_progress || '-'}
                      </p>
                      <p>
                        <b>冲突:</b> {item.decision?.conflict || '无'}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </article>

          <article className="card scene-controls">
            <header className="card-head">
              <h2>流程控制</h2>
              <span className="muted">开始 / 暂停 / 继续</span>
            </header>

            <form className="start-form" onSubmit={handleStart}>
              <label>
                Scene ID
                <input
                  className="text-input"
                  value={startForm.scene_id}
                  onChange={(event) =>
                    setStartForm((prev) => ({ ...prev, scene_id: event.target.value }))
                  }
                  required
                />
              </label>
              <label>
                标题
                <input
                  className="text-input"
                  value={startForm.title}
                  onChange={(event) => setStartForm((prev) => ({ ...prev, title: event.target.value }))}
                  required
                />
              </label>
              <label>
                目标
                <input
                  className="text-input"
                  value={startForm.objective}
                  onChange={(event) =>
                    setStartForm((prev) => ({ ...prev, objective: event.target.value }))
                  }
                  required
                />
              </label>
              <label>
                参与角色（逗号分隔）
                <input
                  className="text-input"
                  value={startForm.participants}
                  onChange={(event) =>
                    setStartForm((prev) => ({ ...prev, participants: event.target.value }))
                  }
                  placeholder="hero,villain"
                  required
                />
              </label>
              <label>
                场景上下文
                <textarea
                  className="text-input"
                  rows={3}
                  value={startForm.context}
                  onChange={(event) => setStartForm((prev) => ({ ...prev, context: event.target.value }))}
                />
              </label>
              <label>
                初始状态 JSON
                <textarea
                  className="text-input mono"
                  rows={4}
                  value={startForm.state}
                  onChange={(event) => setStartForm((prev) => ({ ...prev, state: event.target.value }))}
                />
              </label>
              <label>
                最大回合
                <input
                  className="text-input"
                  type="number"
                  min={1}
                  max={200}
                  value={startForm.max_turns}
                  onChange={(event) =>
                    setStartForm((prev) => ({ ...prev, max_turns: Number(event.target.value) }))
                  }
                />
              </label>

              <div className="control-row">
                <button type="submit" className="btn primary" disabled={controlBusy}>
                  开始执行
                </button>
              </div>
            </form>

            <label>
              控制消息
              <input
                className="text-input"
                value={controlMessage}
                onChange={(event) => setControlMessage(event.target.value)}
                placeholder="可选备注"
              />
            </label>

            <div className="control-row">
              <button type="button" className="btn warn" onClick={handlePause} disabled={controlBusy}>
                暂停
              </button>
              <button type="button" className="btn" onClick={handleResume} disabled={controlBusy}>
                继续
              </button>
            </div>
          </article>
        </section>
      ) : null}

      {tab === 'agents' ? (
        <section className="panel-grid">
          <article className="card span-two">
            <header className="card-head">
              <h2>角色推进状态</h2>
              <span className="muted">按活跃回合数排序</span>
            </header>
            {agents.length === 0 ? (
              <p className="muted">暂无角色进度数据。</p>
            ) : (
              <div className="agent-grid">
                {agents.map((item) => (
                  <div key={item.agent_id} className="agent-card">
                    <h3>{item.agent_id}</h3>
                    <p>
                      <b>活跃回合:</b> {item.turn_count}
                    </p>
                    <p>
                      <b>最近动作:</b> {item.last_action || '-'}
                    </p>
                    <p>
                      <b>目标推进:</b> {item.last_goal_progress || '-'}
                    </p>
                    <p>
                      <b>记忆事件:</b> {item.memory_events}
                    </p>
                    <p>
                      <b>最近记忆:</b> {item.memory_last_content || '-'}
                    </p>
                    <p>
                      <b>最近活跃:</b> {formatLocalTime(item.last_active_at)}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </article>
        </section>
      ) : null}
    </div>
  )
}

function KpiCard({ title, value, hint }) {
  return (
    <article className="kpi-card">
      <p>{title}</p>
      <h3>{value}</h3>
      <span>{hint}</span>
    </article>
  )
}

function StatusPill({ status }) {
  const normalized = String(status || 'ready').toLowerCase()
  return <span className={`status-pill ${normalized}`}>{normalized}</span>
}
