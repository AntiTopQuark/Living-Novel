async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers ?? {}),
    },
    ...options,
  })

  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    const detail = payload?.detail || `Request failed: ${response.status}`
    throw new Error(detail)
  }

  return payload
}

export const dashboardApi = {
  getKpis() {
    return request('/api/dashboard/kpis')
  },
  getScenes() {
    return request('/api/dashboard/scenes')
  },
  getSceneTurns(sceneId) {
    return request(`/api/dashboard/scenes/${encodeURIComponent(sceneId)}/turns`)
  },
  getAgents() {
    return request('/api/dashboard/agents')
  },
  getCosts(params = {}) {
    const search = new URLSearchParams()
    if (params.from) search.set('from', params.from)
    if (params.to) search.set('to', params.to)
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request(`/api/dashboard/costs${suffix}`)
  },
  startScene(payload) {
    return request('/api/control/scenes/start', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  pauseScene(sceneId, message = '') {
    return request(`/api/control/scenes/${encodeURIComponent(sceneId)}/pause`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    })
  },
  resumeScene(sceneId, message = '') {
    return request(`/api/control/scenes/${encodeURIComponent(sceneId)}/resume`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    })
  },
}
