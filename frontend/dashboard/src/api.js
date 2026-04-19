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

function withQuery(path, params = {}) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.set(key, String(value))
  })
  const suffix = search.toString() ? `?${search.toString()}` : ''
  return `${path}${suffix}`
}

export const dashboardApi = {
  getBooks() {
    return request('/api/books')
  },
  createBook(payload) {
    return request('/api/books', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  activateBook(bookId) {
    return request(`/api/books/${encodeURIComponent(bookId)}/activate`, {
      method: 'POST',
    })
  },
  getBookProfile(bookId) {
    return request(`/api/books/${encodeURIComponent(bookId)}/profile`)
  },
  patchBookProfile(bookId, payload) {
    return request(`/api/books/${encodeURIComponent(bookId)}/profile`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },
  getInteractiveSettings(bookId) {
    return request(`/api/books/${encodeURIComponent(bookId)}/interactive-settings`)
  },
  patchInteractiveSettings(bookId, payload) {
    return request(`/api/books/${encodeURIComponent(bookId)}/interactive-settings`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    })
  },
  getKpis(bookId) {
    return request(withQuery('/api/dashboard/kpis', { book_id: bookId }))
  },
  getScenes(bookId) {
    return request(withQuery('/api/dashboard/scenes', { book_id: bookId }))
  },
  getSceneTurns(sceneId, bookId) {
    return request(
      withQuery(`/api/dashboard/scenes/${encodeURIComponent(sceneId)}/turns`, { book_id: bookId }),
    )
  },
  getAgents(bookId) {
    return request(withQuery('/api/dashboard/agents', { book_id: bookId }))
  },
  getCosts(params = {}) {
    return request(
      withQuery('/api/dashboard/costs', {
        book_id: params.book_id,
        scope: params.scope,
        from: params.from,
        to: params.to,
      }),
    )
  },
  startScene(payload) {
    return request('/api/control/scenes/start', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  startSceneAsync(payload) {
    return request('/api/control/scenes/start_async', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  getSceneRun(sceneId, bookId) {
    return request(
      withQuery(`/api/control/scenes/${encodeURIComponent(sceneId)}/run`, { book_id: bookId }),
    )
  },
  interruptScene(sceneId, payload) {
    return request(`/api/control/scenes/${encodeURIComponent(sceneId)}/interrupt`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  getPendingDecision(sceneId, bookId) {
    return request(
      withQuery(`/api/control/scenes/${encodeURIComponent(sceneId)}/decisions/pending`, {
        book_id: bookId,
      }),
    )
  },
  selectDecision(sceneId, requestId, payload) {
    return request(
      `/api/control/scenes/${encodeURIComponent(sceneId)}/decisions/${encodeURIComponent(requestId)}/select`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    )
  },
  pauseScene(sceneId, bookId, message = '') {
    return request(`/api/control/scenes/${encodeURIComponent(sceneId)}/pause`, {
      method: 'POST',
      body: JSON.stringify({ book_id: bookId, message }),
    })
  },
  resumeScene(sceneId, bookId, message = '') {
    return request(`/api/control/scenes/${encodeURIComponent(sceneId)}/resume`, {
      method: 'POST',
      body: JSON.stringify({ book_id: bookId, message }),
    })
  },
}
