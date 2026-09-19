import React, { useEffect, useState } from 'react'
import { api } from './api'
import { useStore } from './store'
import MapView from './components/MapView.jsx'
import BattleView from './components/BattleView.jsx'
import RewardView from './components/RewardView.jsx'
import ForgeView from './components/ForgeView.jsx'
import ShopView from './components/ShopView.jsx'
import DeckView from './components/DeckView.jsx'
import ReplayPlayer from './components/ReplayPlayer.jsx'

export default function App() {
  const { view, setCards, cards, runId, setRunId, applyRun } = useStore()
  const [seed, setSeed] = useState('')
  const [resumeId, setResumeId] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  // 商店面板仅在本地收起：再次进入节点或点击“回到商店”重开（不发任何交易动作）
  const [shopDismissed, setShopDismissed] = useState(false)
  // 整局回放：replayId 非 null 时覆盖全屏播放器（严格只读，与当前对局隔离）
  const [replayId, setReplayId] = useState(null)

  useEffect(() => {
    api.cards().then(setCards).catch(() => {})
  }, [])

  // 切换到不同节点（进商店/离开商店/续局）时重置商店面板的本地收起状态
  useEffect(() => {
    setShopDismissed(false)
  }, [view?.position, runId])

  async function create() {
    setLoading(true); setErr('')
    try {
      const run = await api.createRun(seed ? Number(seed) : undefined)
      applyRun(run)
      setRunId(run.run_id)
    } catch (e) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }

  async function resume() {
    if (!resumeId) return
    setLoading(true); setErr('')
    try {
      const run = await api.resume(resumeId)
      applyRun(run)
      setRunId(run.run_id)
      setReplayId(null)
    } catch (e) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }

  function openReplay(id) {
    const target = (id || runId || '').trim()
    if (target) setReplayId(target)
  }

  async function refreshRun() {
    if (!runId) return
    setErr('')
    try {
      const run = await api.resume(runId)
      applyRun(run)
    } catch (e) {
      setErr(e.message)
    }
  }

  async function newRun() {
    setErr('')
    const run = await api.createRun()
    applyRun(run)
    setRunId(run.run_id)
  }

  if (!view) {
    return (
      <div className="screen home">
        <div className="panel">
          <h1>卡牌闯关</h1>
          <p>选择路线 · 构筑牌组 · 挑战首领 · 失败解锁新卡</p>
          <div className="fieldrow">
            <span>种子（可选）</span>
            <input value={seed} onChange={(e) => setSeed(e.target.value)} placeholder="随机" />
          </div>
          <button className="primary" onClick={create} disabled={loading}>
            {loading ? '创建中…' : '新建一局（随机种子）'}
          </button>
          <div className="divider" />
          <div className="fieldrow">
            <span>续局 ID</span>
            <input value={resumeId} onChange={(e) => setResumeId(e.target.value)} placeholder="粘贴 run_id" />
            <button onClick={resume} disabled={loading}>续局</button>
          </div>
          <div className="divider" />
          <div className="fieldrow">
            <span>回放 ID</span>
            <input
              value={resumeId}
              onChange={(e) => setResumeId(e.target.value)}
              placeholder="粘贴 run_id，逐步播放整局"
              onKeyDown={(e) => e.key === 'Enter' && openReplay(resumeId)}
            />
            <button onClick={() => openReplay(resumeId)} disabled={loading || !resumeId}>🎬 整局回放</button>
          </div>
          {err && <div className="error">{err}</div>}
        </div>
        {cards.length > 0 && <DeckView mode="extras" />}
      </div>
    )
  }

  const hasReward = !view.reward_claimed && view.reward_options && view.reward_options.length > 0
  const hasForge = view.forge_available === true
  const atShop = view.shop_available === true && view.shop
  const showShop = atShop && !shopDismissed
  const ended = view.status === 'won' || view.status === 'lost'

  return (
    <div className="screen">
      <header className="topbar">
        <span className="brand">卡牌闯关</span>
        <span>生命 {view.health}/{view.max_health}</span>
        <span>金币 {view.gold}</span>
        <span>牌组 {view.deck.length}</span>
        <span className="sub">种子 {view.status === 'in_progress' && '#'}{view.seed === undefined ? '' : view.seed}</span>
        <button className="mini" onClick={refreshRun}>刷新</button>
        <button className="mini" onClick={() => openReplay(runId)} title="逐步播放、暂停、跳转整局（只读）">
          🎬 回放本局
        </button>
        <button className="mini" onClick={newRun}>新局</button>
      </header>

      {ended && (
        <div className="overlay">
          <div className="endcard">
            <h2>{view.status === 'won' ? '🎉 通关！' : '💀 失败'}</h2>
            <p>{view.status === 'lost' && '失败解锁了一张新卡。'}</p>
            <DeckView />
            <div className="fieldrow"><button className="primary" onClick={newRun}>再来一局</button></div>
          </div>
        </div>
      )}

      <div className="content">
        <div className="leftcol">
          <DeckView />
          {view.unlocked_cards && <Unlocks unlocked={view.unlocked_cards} />}
        </div>
        <div className="maincol">
          {view.in_battle ? (
            <BattleView view={view} />
          ) : (
            <MapView view={view} />
          )}
          {atShop && shopDismissed && !ended && (
            <div className="shopreopen">
              <button className="primary" onClick={() => setShopDismissed(false)}>🛒 回到商店</button>
            </div>
          )}
          {hasReward && !ended && <RewardView view={view} />}
          {hasForge && !ended && <ForgeView view={view} />}
          {showShop && !ended && <ShopView view={view} onClose={() => setShopDismissed(true)} />}
        </div>
      </div>

      {replayId && (
        <ReplayPlayer runId={replayId} onClose={() => setReplayId(null)} />
      )}

      {err && <div className="error toast">{err}</div>}
    </div>
  )
}

function Unlocks({ unlocked }) {
  const { cardMeta } = useStore()
  return (
    <div className="panellist">
      <h3>已解锁卡</h3>
      <div className="unlockgrid">
        {unlocked.unlocked.map((id) => {
          const c = cardMeta(id)
          return <span key={id} className="chip">{c ? c.name : id}</span>
        })}
      </div>
    </div>
  )
}