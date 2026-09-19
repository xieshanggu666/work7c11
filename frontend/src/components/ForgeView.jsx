import React, { useMemo, useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'

const BRANCH_TAG = { sharpen: '锋', empower: '强', refine: '炼' }

// 锻造节点：花金币为一张指定卡牌实例选择强化分支（每节点限一次）
export default function ForgeView({ view }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(null) // 卡牌实例 uid
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)
  const cardMeta = useStore((s) => s.cardMeta)

  const cost = view.forge_cost ?? 25
  const canAfford = view.gold >= cost
  const branches = view.forge_branches || []

  // 同名卡按实例列出，各自显示已选分支
  const instances = useMemo(
    () => (view.deck || []).map((d) => ({ ...d, meta: cardMeta(d.id) })),
    [view.deck, cardMeta],
  )

  async function forge(branch) {
    if (!selected || busy) return
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, { action: 'forge', card: selected, branch })
      applyRun(res.run)
      setSelected(null)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="overlay">
      <div className="forgecard panel">
        <h2>🔨 锻造台</h2>
        <p className="forgedesc">
          选择一张卡牌，花费 <b>{cost}</b> 金币为其选择一条强化分支。
          同名卡各自独立成长，每张仅受自己的锻造影响。
        </p>
        <div className="forgelist">
          {instances.map((inst) => {
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
                <span className="cname">
                  {c.name}
                  {inst.forges.length > 0 && (
                    <em className="ftags">
                      {inst.forges.map((f, i) => (
                        <i key={i} className={`ftag ${f}`}>{BRANCH_TAG[f] || f}</i>
                      ))}
                    </em>
                  )}
                </span>
                <span className="cdesc">{c.desc}</span>
              </button>
            )
          })}
        </div>

        <div className="forgebranches">
          {branches.map((b) => (
            <button
              key={b.id}
              className="fbranch"
              onClick={() => forge(b.id)}
              disabled={!selected || !canAfford || busy}
              title={b.desc}
            >
              <span className={`ftag big ${b.id}`}>{b.tag}</span>
              <span className="bname">{b.name}</span>
              <span className="bdesc">{b.desc}</span>
            </button>
          ))}
        </div>
        {!canAfford && <div className="error">金币不足（需要 {cost}，当前 {view.gold}）。</div>}
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}
