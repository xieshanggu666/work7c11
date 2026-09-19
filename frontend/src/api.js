const BASE = '/api'

// 行动请求并发控制：
// - expectedRev：当前视口的存档版本号，服务端据此拒绝基于过期状态的提交（409 状态冲突）
// - 每个新意图生成 request_id 做请求级幂等；网络超时后的同一重试复用同一 id，
//   服务端返回首次结果，不会重复扣款/发奖（双击/重复请求同理）
let expectedRev = null

export function setExpectedRev(rev) {
  if (Number.isInteger(rev)) expectedRev = rev
}

export class ConflictError extends Error {
  constructor(detail) {
    super(detail || '状态已变化，请刷新后重试')
    this.status = 409
  }
}

let reqSeq = 0
function newRequestId() {
  reqSeq += 1
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return `${Date.now().toString(36)}-${crypto.randomUUID().slice(0, 8)}`
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}-${reqSeq}`
}

async function j(url, opts) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    if (res.status === 409) throw new ConflictError(data.detail)
    throw new Error(data.detail || `HTTP ${res.status}`)
  }
  return data
}

export const api = {
  cards: () => j(`${BASE}/cards`),
  async createRun(seed) {
    const data = await j(`${BASE}/runs`, { method: 'POST', body: JSON.stringify({ seed }) })
    setExpectedRev(data.rev)
    return data
  },
  async resume(id) {
    const data = await j(`${BASE}/runs/${id}/resume`)
    setExpectedRev(data.rev)
    return data
  },
  replay: (id) => j(`${BASE}/runs/${id}/replay`),

  act: async (id, action, { retryKey } = {}) => {
    // retryKey：调用方在“重试同一个意图”时显式传入；缺省每个调用一个新令牌
    const requestId = retryKey || newRequestId()
    const body = { ...action, request_id: requestId }
    if (expectedRev !== null) body.expected_rev = expectedRev
    try {
      const data = await j(`${BASE}/runs/${id}/act`, { method: 'POST', body: JSON.stringify(body) })
      if (Number.isInteger(data.rev)) expectedRev = data.rev
      return data
    } catch (e) {
      // 状态冲突：版本号已失效，清掉避免后续请求继续带旧值；调用方应刷新续局
      if (e instanceof ConflictError) expectedRev = null
      throw e
    }
  },
}

// 统一的行动错误处理：遇到 409（重复请求/状态冲突）自动拉取最新视口对齐，
// 返回可展示给用户的提示语。refresh 为最新视口应用函数（通常是 applyRun）。
export async function handleActError(e, runId, refresh) {
  if (e instanceof ConflictError) {
    try {
      const fresh = await api.resume(runId)
      if (refresh) refresh(fresh)
    } catch (_) { /* 刷新失败仅保留提示 */ }
    return '操作与最新状态冲突，已自动刷新，请重试'
  }
  return e.message
}
