import React from 'react'

const TYPE_LABEL = {
  encounter: '遭遇', elite: '精英', rest: '休息', reward: '奖励',
  forge: '锻造', shop: '商店', boss: '首领', start: '营地',
}

// 回放专用只读地图：不可点击推进；当前位置高亮，已访问节点（沿位置之前的行）标记。
export default function ReplayMap({ view }) {
  const m = view.map
  const position = view.position

  const order = [m.start, ...Object.keys(m.nodes || {})
    .filter((n) => n !== m.start && n !== 'boss')
    .sort((a, b) => {
      const ra = Number(a.split('-')[0]); const rb = Number(b.split('-')[0])
      if (ra !== rb) return ra - rb
      return Number(a.split('-')[1]) - Number(b.split('-')[1])
    }), m.boss]

  const rows = []
  for (const nid of order) {
    const node = m.nodes[nid]
    const nrow = node.type === 'start' ? -1 : node.type === 'boss' ? m.rows : Number(nid.split('-')[0])
    rows.push({ nid, node, nrow })
  }
  const byRow = {}
  rows.forEach((r) => { (byRow[r.nrow] = byRow[r.nrow] || []).push(r) })

  const curRow = m.nodes[position]?.row
  return (
    <div className="map replay-map">
      <h3>路线回放（当前节点：{TYPE_LABEL[m.nodes[position]?.type] || position}）</h3>
      <div className="mapgrid">
        {Object.keys(byRow).sort((a, b) => Number(a) - Number(b)).map((r) => (
          <div className="maprow" key={r}>
            {byRow[r].map(({ nid, node }) => {
              const isCur = nid === position
              const visited = typeof curRow === 'number' && Number(r) < curRow
              return (
                <span
                  key={nid}
                  className={`mnode ${node.type} ${isCur ? 'cur replay-cur' : ''} ${visited ? 'visited' : ''} readonly`}
                >
                  <span className="mlabel">{TYPE_LABEL[node.type]}</span>
                  <span className="msub">{isCur ? '●' : visited ? '·' : node.enemy || ''}</span>
                </span>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
