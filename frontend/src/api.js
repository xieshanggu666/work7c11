const BASE = '/api'

async function j(url, opts) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data.detail || `HTTP ${res.status}`)
  }
  return data
}

export const api = {
  cards: () => j(`${BASE}/cards`),
  createRun: (seed) => j(`${BASE}/runs`, { method: 'POST', body: JSON.stringify({ seed }) }),
  resume: (id) => j(`${BASE}/runs/${id}/resume`),
  act: (id, action) => j(`${BASE}/runs/${id}/act`, { method: 'POST', body: JSON.stringify(action) }),
  replay: (id) => j(`${BASE}/runs/${id}/replay`),
}