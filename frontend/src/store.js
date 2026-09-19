import { create } from 'zustand'
import { api, newReqId } from './api'

const initialState = {
  cards: [],            // 全部卡牌元数据
  runId: null,
  view: null,           // 服务端 _public_view
  meta: { cards: [], enemies: [] },
  log: [],
  playing: false,
  error: null,
}

export const useStore = create((set, get) => ({
  ...initialState,

  setCards: (cards) => set({ cards }),

  applyRun: (run) => set({ view: run, runId: run.run_id }),

  setRunId: (id) => set({ runId: id }),

  setMeta: (meta) => set({ meta }),

  setLog: (log) => set({ log }),

  setError: (err) => set({ error: err }),

  setPlaying: (playing) => set({ playing }),

  cardMeta: (id) => get().cards.find((c) => c.id === id) || null,

  // 统一行动提交：每个“逻辑操作”只发一个 req_id 令牌（双击/重试复用），
  // 服务端按令牌幂等去重；409 状态冲突（并发请求已推进存档）时自动拉取
  // /resume 权威视口并抛出可读错误，调用方提示用户在最新状态上重试。
  // 返回原始响应，由调用方自行决定何时 applyRun（战斗需先播放结算动画再落帧）。
  submitAction: async (actionBody) => {
    const id = get().runId
    const reqId = newReqId()
    const send = () => api.act(id, actionBody, reqId)
    try {
      return await send()
    } catch (e) {
      if (e.isStateConflict) {
        try {
          get().applyRun(await api.resume(id))
        } catch {
          /* 同步失败时保留原始冲突错误 */
        }
        throw new Error('操作冲突：存档已被其他请求推进，已同步到最新状态，请重试')
      }
      // 网络层失败（未拿到 HTTP 响应）：用同一 req_id 重试一次，
      // 即使首次请求实际已在服务端生效，也只会回放首次结果，绝不重复生效
      if (e.status === undefined) {
        try {
          return await send()
        } catch (e2) {
          if (e2.isStateConflict) {
            try {
              get().applyRun(await api.resume(id))
            } catch {
              /* ignore */
            }
            throw new Error('操作冲突：存档已被其他请求推进，已同步到最新状态，请重试')
          }
          throw e2
        }
      }
      throw e
    }
  },
}))

// 手牌/牌组项兼容两种形态：旧档裸 id（字符串）或卡牌实例 {uid,id,cost,forges}
export function cardRef(item) {
  return typeof item === 'string' ? item : item?.uid
}

export function cardIdOf(item) {
  return typeof item === 'string' ? item : item?.id
}

// 服务端卡牌效果标签 -> 中文简介
export function cardBadge(card) {
  if (!card) return ''
  switch (card.type) {
    case 'attack': return '攻击'
    case 'skill': return '技能'
    case 'power': return '能力'
    default: return ''
  }
}