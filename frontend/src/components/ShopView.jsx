import React, { useMemo, useState } from 'react'
import { useStore } from '../store'

const TYPE_LABEL = { attack: '攻击', skill: '技能', power: '能力' }

// 旅途商店：购买卡牌/遗物，或付费移除指定卡牌实例。
// 服务端统一处理扣款/售罄/失败回退，本组件只发动作并应用返回视口；
// “启程”仅在本地收起面板（不发动作），点击地图上的当前节点可再次打开。
export default function ShopView({ view, onClose }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(null) // 待移除的卡牌实例 uid
  const applyRun = useStore((s) => s.applyRun)
  const submitAction = useStore((s) => s.submitAction)
  const cardMeta = useStore((s) => s.cardMeta)

  const shop = view.shop
  const removeCost = shop?.remove?.cost ?? 0
  const canRemoveAfford = view.gold >= removeCost

  const deck = useMemo(
    () => (view.deck || []).map((d) => ({ ...d, meta: cardMeta(d.id) })),
    [view.deck, cardMeta],
  )

  async function transact(body) {
    setBusy(true); setErr('')
    try {
      const res = await submitAction(body)
      applyRun(res.run)
      setSelected(null)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  function buy(kind, sku) {
    return transact({ action: 'shop_buy', kind, sku })
  }

  // 交易记录里把 sku 解析成中文名称
  const itemName = (kind, sku) => {
    const bucket = kind === 'card' ? shop.cards : shop.relics
    return bucket.find((it) => it.sku === sku)?.name || sku
  }

  return (
    <div className="overlay">
      <div className="shopcard panel">
        <h2>🛒 旅途商店</h2>
        <p className="shopgold">金币 <b>{view.gold}</b></p>

        <h3 className="shopsection">卡牌</h3>
        <div className="shoplist">
          {shop.cards.length === 0 && <span className="shopempty">卡牌已售罄。</span>}
          {shop.cards.map((it) => (
            <button
              key={it.sku}
              className={`shopitem ${it.tier} ${it.sold ? 'sold' : ''}`}
              onClick={() => !it.sold && buy('card', it.sku)}
              disabled={it.sold || busy || view.gold < it.price}
              title={it.desc}
            >
              <span className="siname">
                {it.name}
                <em className="sitype">{TYPE_LABEL[it.type]} · {it.card_cost} 费</em>
              </span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </button>
          ))}
        </div>

        <h3 className="shopsection">遗物</h3>
        <div className="shoplist">
          {shop.relics.length === 0 && <span className="shopempty">遗物已售罄。</span>}
          {shop.relics.map((it) => (
            <button
              key={it.sku}
              className={`shopitem relic ${it.sold ? 'sold' : ''}`}
              onClick={() => !it.sold && buy('relic', it.sku)}
              disabled={it.sold || busy || view.gold < it.price}
              title={it.desc}
            >
              <span className="siname">📿 {it.name}</span>
              <span className="sidesc">{it.desc}</span>
              <span className="siprice">{it.sold ? '已售出' : `${it.price} 金币`}</span>
            </button>
          ))}
        </div>

        <h3 className="shopsection">移除服务</h3>
        <p className="shopdesc">
          付费永久移除一张指定卡牌实例（同名卡的其余副本不受影响）。
          本次价格 <b>{removeCost}</b> 金币，每移除一次价格上涨
          {' '}{shop.remove.next_cost - removeCost} 金币。
        </p>
        <div className="forgelist">
          {deck.map((inst) => {
            const c = inst.meta || { name: inst.id, desc: '', tier: '' }
            const isSel = selected === inst.uid
            return (
              <button
                key={inst.uid}
                className={`forgeinst ${c.tier} ${isSel ? 'sel' : ''}`}
                onClick={() => setSelected(inst.uid)}
                disabled={busy}
                title={c.desc}
              >
                <span className="cname">{c.name}</span>
                <span className="cdesc">{c.desc}</span>
              </button>
            )
          })}
        </div>
        <button
          className="primary"
          onClick={() => transact({ action: 'shop_remove', card: selected })}
          disabled={!selected || !canRemoveAfford || busy}
        >
          移除选中卡牌（{removeCost} 金币）
        </button>
        {!canRemoveAfford && (
          <div className="error">金币不足（需要 {removeCost}，当前 {view.gold}）。</div>
        )}

        {shop.tx.length > 0 && (
          <div className="shoptx">
            <h3 className="shopsection">交易记录</h3>
            <ul>
              {shop.tx.map((t, i) => (
                <li key={i}>
                  {t.type === 'buy'
                    ? `购入${t.kind === 'card' ? '卡牌' : '遗物'}「${itemName(t.kind, t.sku)}」，花费 ${t.price}`
                    : `移除卡牌实例（${cardMeta(t.card)?.name || t.card}），花费 ${t.price}，牌组 ${t.deck_size} 张`}
                  ｜余额 {t.gold_left}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="divider" />
        <button className="primary" onClick={onClose} disabled={busy}>启程 →</button>
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
