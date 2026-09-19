import React, { useEffect, useRef } from 'react'
import { useStore } from '../store'
import { bus } from '../phaser/battleBus'
import { startPhaser } from '../phaser/BattleScene.js'
import ReplayMap from './ReplayMap.jsx'

const TYPE_LABEL = {
  encounter: '遭遇', elite: '精英', rest: '休息', reward: '奖励',
  forge: '锻造', shop: '商店', boss: '首领', start: '营地',
}
const BRANCH_TAG = { sharpen: '锋', empower: '强', refine: '炼' }

// 整局回放的单帧舞台：严格只读，不调用 api.act。
// - 地图帧：高亮当前位置与已走路径
// - 战斗帧：Phaser 落到该帧权威快照；播放时逐行动画由父组件经 bus 推送
// - 锻造/奖励/商店帧：渲染该帧重建出的面板状态（金币、分支、货架/交易记录）
export default function ReplayStage({ view, showShop }) {
  const mountRef = useRef(null)
  const gameRef = useRef(null)
  const viewRef = useRef(view)
  viewRef.current = view
  const cardMeta = useStore((s) => s.cardMeta)

  useEffect(() => {
    gameRef.current = startPhaser(mountRef.current)
    const off = bus.on('phaser_ready', () => {
      const v = viewRef.current
      bus.emit('snapshot', (v && v.battle) || { player: null, enemy: null })
    })
    return () => {
      bus.clear()
      off()
      gameRef.current?.destroy(true)
      gameRef.current = null
    }
  }, [])

  useEffect(() => {
    // 跳转/单步：立即落到权威快照（不放动画）
    bus.emit('snapshot', view.battle || { player: null, enemy: null })
  }, [view])

  if (view.in_battle && view.battle) {
    const b = view.battle
    const hand = b.hand || []
    return (
      <div className="battle replay-battle">
        <div ref={mountRef} className="phaser" />
        <div className="handbar replay-handbar">
          <div className="energy">能量 {b.energy} / {b.max_energy}</div>
          <div className="hand">
            {hand.length === 0 && <span className="hint">手牌为空</span>}
            {hand.map((item) => {
              const hc = typeof item === 'string'
                ? { uid: item, id: item, cost: cardMeta(item)?.cost ?? 0, forges: [] }
                : { uid: item.uid, id: item.id, cost: item.cost ?? cardMeta(item.id)?.cost ?? 0, forges: item.forges || [] }
              const c = cardMeta(hc.id) || { name: hc.id, type: 'attack', desc: '' }
              return (
                <span key={hc.uid} className={`card ${c.type} replayed ${hc.forges.length ? 'forged' : ''}`} title={c.desc}>
                  <span className="ccost">{hc.cost}</span>
                  <span className="cname">{c.name}</span>
                  {hc.forges.length > 0 && (
                    <span className="handforges">
                      {hc.forges.map((f, i) => (
                        <i key={i} className={`ftag ${f}`}>{BRANCH_TAG[f] || f}</i>
                      ))}
                    </span>
                  )}
                </span>
              )
            })}
          </div>
          <span className="replay-badge">回放 · 回合 {b.turn}{b.in_turn ? ' · 玩家回合' : ''}</span>
        </div>
      </div>
    )
  }

  const node = view.map?.nodes?.[view.position]
  return (
    <div className="replay-stage">
      <ReplayMap view={view} />
      {node?.type === 'forge' && <ForgeSnapshot view={view} />}
      {node?.type === 'reward' && <RewardSnapshot view={view} />}
      {showShop && view.shop && <ShopSnapshot view={view} />}
      {view.status === 'won' && <div className="replay-end won">🏆 本帧：通关</div>}
      {view.status === 'lost' && <div className="replay-end lost">💀 本帧：战败（回放不发放解锁）</div>}
      <div className="replay-route-meta">
        <span>生命 {view.health}/{view.max_health}</span>
        <span>金币 {view.gold}</span>
        <span>牌组 {view.deck.length}</span>
        <span>{node ? TYPE_LABEL[node.type] : view.position}</span>
      </div>
    </div>
  )
}

function ForgeSnapshot({ view }) {
  const cardMeta = useStore((s) => s.cardMeta)
  const forgedUids = new Set(view.deck.filter((d) => d.forges?.length).map((d) => d.uid))
  return (
    <div className="overlay replay-overlay">
      <div className="forgecard panel replay-panel">
        <h2>🔨 锻造台（回放）</h2>
        <p className="forgedesc">
          锻造节点状态：{view.forge_claimed ? '已完成锻造（或离开）' : '尚未锻造'}。
          下列卡牌实例携带各自在本局累计的锻造分支。
        </p>
        <div className="forgelist">
          {view.deck.map((inst) => {
            const c = cardMeta(inst.id) || { name: inst.id, desc: '', tier: '' }
            return (
              <span key={inst.uid} className={`forgeinst ${c.tier} ${forgedUids.has(inst.uid) ? 'sel' : ''} readonly`}>
                <span className="cname">
                  {c.name}
                  {inst.forges?.length > 0 && (
                    <em className="ftags">
                      {inst.forges.map((f, i) => (
                        <i key={i} className={`ftag ${f}`}>{BRANCH_TAG[f] || f}</i>
                      ))}
                    </em>
                  )}
                </span>
                <span className="cdesc">{c.desc}</span>
              </span>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function RewardSnapshot({ view }) {
  return (
    <div className="overlay replay-overlay">
      <div className="rewardcard panel replay-panel">
        <h2>奖励（回放）</h2>
        {view.reward_claimed ? (
          <p className="shopdesc">奖励已在之前/本帧领取。</p>
        ) : (
          <div className="rewardopts">
            {(view.reward_options || []).map((o, i) => (
              <span key={i} className="roption readonly">
                <span className="rkind">{o.name}</span>
                <span className="rdesc">{o.desc}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function ShopSnapshot({ view }) {
  const shop = view.shop
  const cardMeta = useStore((s) => s.cardMeta)
  const nameOf = (kind, sku) => (kind === 'card' ? shop.cards : shop.relics).find((it) => it.sku === sku)?.name || sku
  return (
    <div className="overlay replay-overlay">
      <div className="shopcard panel replay-panel">
        <h2>🛒 旅途商店（回放）</h2>
        <p className="shopgold">本帧金币 <b>{view.gold}</b>（只读）</p>

        <h3 className="shopsection">卡牌货架</h3>
        <div className="shoplist">
          {shop.cards.length === 0 && <span className="shopempty">无货架</span>}
          {shop.cards.map((it) => (
            <span key={it.sku} className={`shopitem ${it.tier} readonly ${it.sold ? 'sold' : ''}`}>
              <span className="siname">{it.name}<em className="sitype">{it.type} · {it.card_cost} 费</em></span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </span>
          ))}
        </div>

        <h3 className="shopsection">遗物货架</h3>
        <div className="shoplist">
          {shop.relics.map((it) => (
            <span key={it.sku} className={`shopitem relic readonly ${it.sold ? 'sold' : ''}`}>
              <span className="siname">📿 {it.name}</span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </span>
          ))}
        </div>

        {shop.tx.length > 0 && (
          <div className="shoptx">
            <h3 className="shopsection">已重建的交易记录（{shop.tx.length}）</h3>
            <ul>
              {shop.tx.map((t, i) => (
                <li key={i}>
                  {t.type === 'buy'
                    ? `购入${t.kind === 'card' ? '卡牌' : '遗物'}「${nameOf(t.kind, t.sku)}」，花费 ${t.price}`
                    : `移除卡牌实例（${cardMeta(t.card)?.name || t.card}），花费 ${t.price}，牌组 ${t.deck_size} 张`}
                  ｜余额 {t.gold_left}
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="shopdesc">移除服务：本次价格 {shop.remove.cost}，已用 {shop.remove.used} 次。</p>
      </div>
    </div>
  )
}
