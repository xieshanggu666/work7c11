import React, { useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'

export default function RewardView({ view }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const runId = useStore((s) => s.runId)
  const applyRun = useStore((s) => s.applyRun)

  async function claim(idx) {
    setBusy(true); setErr('')
    try {
      const res = await api.act(runId, { action: 'claim_reward', option: idx })
      applyRun(res.run)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="overlay">
      <div className="rewardcard panel">
        <h2>选择奖励</h2>
        <div className="rewardopts">
          {view.reward_options.map((o, i) => (
            <button key={i} className="roption" onClick={() => claim(i)} disabled={busy} title={o.desc}>
              <span className="rkind">{o.name}</span>
              <span className="rdesc">{o.desc}</span>
              <span className="rgo">领取 →</span>
            </button>
          ))}
        </div>
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  )
}