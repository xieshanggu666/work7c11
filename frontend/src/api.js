const BASE = '/api'

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message)
    this.status = status
    this.data = data
  }

  // 409 + “concurrently/revision”：本地视图已过期（另一标签页/请求先提交），应刷新后重试
  get isStateConflict() {
    return this.status === 409 && /concurrent|revision/i.test(this.message || '')
  }
}

let _reqSeq = 0

// 幂等令牌：同一“逻辑操作”的网络重试必须复用同一 req_id，
// 服务端命中后回放首次结果，绝不重复生效/扣款。
export function newReqId() {
  _reqSeq += 1
  const rand = (globalThis.crypto?.randomUUID?.() ||
    Math.random().toString(36).slice(2) + Date.now().toString(36))
  return `${Date.now().toString(36)}-${_reqSeq}-${rand}`
}

async function j(url, opts) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new ApiError(data.detail || `HTTP ${res.status}`, res.status, data)
  }
  return data
}

export const api = {
  cards: () => j(`${BASE}/cards`),
  createRun: (seed) => j(`${BASE}/runs`, { method: 'POST', body: JSON.stringify({ seed }) }),
  resume: (id) => j(`${BASE}/runs/${id}/resume`),
  // action 携带 req_id 幂等令牌：网络失败重传同一逻辑操作时显式复用，
  // 服务端命中后回放首次结果，绝不重复生效/扣款。
  act: (id, action, reqId) =>
    j(`${BASE}/runs/${id}/act`, {
      method: 'POST',
      body: JSON.stringify({ req_id: reqId || newReqId(), ...action }),
    }),
  replay: (id) => j(`${BASE}/runs/${id}/replay`),
}
