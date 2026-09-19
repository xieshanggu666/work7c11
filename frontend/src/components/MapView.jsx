import React, { useState } from 'react'
import { useStore } from '../store'

const TYPE_LABEL = {
  encounter: '遭遇',
  elite: '精英',
  rest: '休息',
  reward: '奖励',
  forge: '锻造',
  shop: '商店',
  boss: '首领',
  start: '起点',
}

export default function MapView({ view }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const submitAction = useStore((s) => s.submitAction)
  const applyRun = useStore((s) => s.applyRun)

  const m = view.map
  const position = view.position

  async function go(node) {
    setBusy(true); setErr('')
    try {
      const res = await submitAction({ action: 'choose_node', node })
      applyRun(res.run)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  const order = [m.start, ...Array.from(new Set(Object.keys(m.nodes || {}).filter((n) => n !== m.start && n !== 'boss'))).sort((a, b) => {
    const ra = Number(a.split('-')[0]); const rb = Number(b.split('-')[0])
    if (ra !== rb) return ra - rb
    return Number(a.split('-')[1]) - Number(b.split('-')[1])
  }), m.boss]

  // 遍历 rows
  const rows = []
  for (const nid of order) {
    const node = m.nodes[nid]
    const nrow = node.type === 'start' ? -1 : node.type === 'boss' ? m.rows : Number(nid.split('-')[0])
    rows.push({ nid, node, nrow })
  }
  const byRow = {}
  rows.forEach((r) => {
    ;(byRow[r.nrow] = byRow[r.nrow] || []).push(r)
  })

  return (
    <div className="map">
      <h3>选择路线（当前节点：{TYPE_LABEL[m.nodes[position]?.type] || position}）</h3>
      <div className="mapgrid">
        {Object.keys(byRow).sort((a, b) => Number(a) - Number(b)).map((r) => (
          <div className="maprow" key={r}>
            {byRow[r].map(({ nid, node }) => {
              const reachable = view.reachable.some((x) => x.id === nid)
              const isCur = nid === position
              return (
                <button
                  key={nid}
                  className={`mnode ${node.type} ${isCur ? 'cur' : ''} ${reachable ? 'reachable' : ''}`}
                  onClick={() => reachable && go(nid)}
                  disabled={!reachable || busy}
                >
                  <span className="mlabel">{TYPE_LABEL[node.type]}</span>
                  <span className="msub">{isCur ? '●' : node.enemy || ''}</span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
      {err && <div className="error">{err}</div>}
    </div>
  )
}