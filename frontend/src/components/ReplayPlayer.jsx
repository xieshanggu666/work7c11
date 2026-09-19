import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { bus } from '../phaser/battleBus'
import ReplayStage from './ReplayStage.jsx'

const KIND_LABEL = {
  create: '建局', route: '路线', battle_entry: '进入战斗', battle: '战斗',
  reward: '奖励', forge: '锻造', trade: '交易', other: '其他',
}
const KIND_ICON = {
  create: '🏕️', route: '🧭', battle_entry: '🚪', battle: '⚔️',
  reward: '🎁', forge: '🔨', trade: '🛒', other: '•',
}
const SPEEDS = [0.5, 1, 2, 4]
// 每个动作帧的基础停留毫秒（倍速缩放）；战斗帧的结算事件另走 Phaser 动画队列
const FRAME_HOLD_MS = 650

// 整局可交互回放：播放 / 暂停 / 上一步 / 下一步 / 拖拽跳转 / 倍速 / 时间轴。
// 数据来自 GET /replay 的 steps（后端已逐步重建状态并校验），本组件只读展示。
export default function ReplayPlayer({ runId, onClose }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [idx, setIdx] = useState(0)          // 当前帧下标（0..steps-1）
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [filter, setFilter] = useState('all')
  const [showShop, setShowShop] = useState(true)
  const timerRef = useRef(null)
  const playingRef = useRef(false)

  useEffect(() => {
    let alive = true
    api.replay(runId).then((d) => { if (alive) setData(d) }).catch((e) => alive && setErr(e.message))
    return () => { alive = false; stopTimer() }
  }, [runId])

  const steps = data?.steps || []
  const step = steps[idx]

  const stopTimer = useCallback(() => {
    playingRef.current = false
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
  }, [])

  const speedRef = useRef(speed)
  speedRef.current = speed
  const stepsRef = useRef(steps)
  stepsRef.current = steps
  const idxRef = useRef(idx)
  idxRef.current = idx

  // 播放某帧的战斗结算事件。舞台在 idx effect 中已把该帧权威快照送入 Phaser，
  // 这里仅把结算事件按序推入队列（queue_done 时 resolve）。
  const playStepEvents = useCallback((s) => {
    const evs = (s?.events || []).filter(Boolean)
    if (!evs.length || !(s.view?.in_battle)) return Promise.resolve()
    return new Promise((resolve) => {
      let done = false
      const finish = () => {
        if (done) return
        done = true
        off()
        clearTimeout(t)
        resolve()
      }
      const off = bus.on('queue_done', finish)
      const t = setTimeout(finish, 12000) // 兜底，避免卡死
      bus.emit('queue', evs)
    })
  }, [])

  const goto = useCallback((next) => {
    const list = stepsRef.current
    const target = Math.max(0, Math.min(list.length - 1, next))
    setIdx(target)
    const s = list[target]
    bus.emit('reset')
    if (s) bus.emit('snapshot', s.view.battle || { player: null, enemy: null })
  }, [])

  const pause = useCallback(() => {
    stopTimer()
    bus.emit('reset')          // 停下尚未播完的结算动画队列
    setPlaying(false)
  }, [stopTimer])

  const play = useCallback(() => {
    if (!steps.length) return
    setPlaying(true)
    playingRef.current = true
    // 在结尾按播放：回到开局重新播
    if (idxRef.current >= steps.length - 1) {
      goto(0)
    }
  }, [steps, goto])

  useEffect(() => () => stopTimer(), [stopTimer])

  // 播放主循环（playing 为真时运行）：切帧 -> 等舞台提交快照 -> 播结算动画 -> 停留 -> 下一帧。
  // 所有状态经 ref 读取，避免闭包陈旧；暂停/卸载通过 playingRef 与 cancelled 退出。
  useEffect(() => {
    if (!playing || !steps.length) return
    let cancelled = false
    const wait = (ms) => new Promise((res) => { timerRef.current = setTimeout(res, ms) })
    let busy = Promise.resolve()

    async function loop() {
      while (playingRef.current && !cancelled) {
        const list = stepsRef.current
        const cur = idxRef.current
        if (cur >= list.length - 1) { setPlaying(false); return }
        const target = cur + 1
        const s = list[target]
        setIdx(target)                                  // 切帧：ReplayStage effect 应用权威快照
        await wait(110)                                 // 等 React 提交 + Phaser 落快照
        if (!playingRef.current || cancelled) return
        // 串行化动画，防止暂停后旧队列的 queue_done 提前唤醒新一轮
        busy = busy.then(() => (playingRef.current ? playStepEvents(s) : Promise.resolve()))
        await busy
        if (!playingRef.current || cancelled) return
        await wait(FRAME_HOLD_MS / speedRef.current)
      }
    }
    loop()
    return () => { cancelled = true }
  }, [playing, steps.length, playStepEvents])

  // 时间轴（可按类型过滤；点击跳转）。route 过滤同时包含“进入战斗”的路线步。
  const timeline = useMemo(() => {
    return steps.map((s, i) => ({ ...s, i })).filter((s) => {
      if (filter === 'all') return true
      if (filter === 'route') return s.kind === 'route' || s.kind === 'battle_entry'
      return s.kind === filter
    })
  }, [steps, filter])

  if (err) {
    return (
      <div className="overlay">
        <div className="replaycard panel">
          <h2>整局回放</h2>
          <div className="error">{err}</div>
          <button className="primary" onClick={onClose}>关闭</button>
        </div>
      </div>
    )
  }
  if (!data) {
    return <div className="overlay"><div className="replaycard panel">加载回放数据…</div></div>
  }

  const v = data.verification
  const progress = steps.length ? ((idx + 1) / steps.length) * 100 : 0

  return (
    <div className="overlay replay-overlay-full">
      <div className="replay-shell">
        <header className="replay-top">
          <div className="replay-title">
            🎬 整局回放
            <span className="replay-seed">种子 #{data.seed}</span>
            <span className="replay-ver" title={`录制版本：${data.recorded_versions.join(', ') || '旧版（无版本号）'}`}>
              规则 v{data.rules_version}
              {data.legacy && <em className="legacy-tag">旧日志兼容</em>}
            </span>
          </div>
          <div className={`replay-verify ${v.final_match ? 'ok' : 'bad'}`}>
            校验点：
            <i className="chk ok">{v.ok} 通过</i>
            {v.legacy > 0 && <i className="chk legacy">{v.legacy} 旧版</i>}
            {v.mismatch > 0 && <i className="chk bad">{v.mismatch} 不一致</i>}
            {v.error > 0 && <i className="chk bad">{v.error} 错误</i>}
            <span className="iso">🔒 只读隔离 · 不写存档 · 不发解锁</span>
          </div>
          <button className="mini" onClick={onClose}>退出回放 ✕</button>
        </header>

        <div className="replay-body">
          <div className="replay-stage-wrap">
            {step && <ReplayStage view={step.view} showShop={showShop} />}
            {step?.check === 'mismatch' && (
              <div className="replay-warn">⚠ 第 {step.seq} 步校验点不一致：录制 {step.check && ''}
                状态哈希与重放结果不同（规则版本变化或日志损坏），显示的是按当前规则重推的状态。</div>
            )}
            {step?.check === 'error' && (
              <div className="replay-warn">⚠ 第 {step.seq} 步无法重放：{step.error}</div>
            )}
            {step?.legacy && (
              <div className="replay-warn legacy">旧版日志步骤：无校验点，按兼容模式重放（不保证逐位一致）。</div>
            )}
          </div>

          <aside className="replay-side">
            <div className="replay-current">
              <div className="rc-kind">
                {KIND_ICON[step?.kind] || '•'} {KIND_LABEL[step?.kind] || step?.kind}
                <span className="rc-seq">#{step?.seq}</span>
              </div>
              <div className="rc-title">{step?.title}</div>
              {step?.summary && <div className="rc-summary">{step.summary}</div>}
              {step?.result && (
                <div className={`rc-result ${step.result}`}>
                  {step.result === 'run_won' ? '🏆 通关' : step.result === 'won' ? '🎉 战斗胜利' : '💀 战斗失败'}
                </div>
              )}
            </div>

            <div className="replay-filters">
              {['all', 'route', 'battle', 'reward', 'forge', 'trade'].map((k) => (
                <button
                  key={k}
                  className={`mini fchip ${filter === k ? 'on' : ''}`}
                  onClick={() => setFilter(k)}
                >
                  {k === 'all' ? '全部' : KIND_LABEL[k]}
                </button>
              ))}
            </div>

            <div className="replay-timeline">
              {timeline.map((s) => (
                <button
                  key={s.seq}
                  className={`tl-item ${s.kind === 'battle_entry' ? 'route' : s.kind} ${s.i === idx ? 'cur' : ''} chk-${s.check}`}
                  onClick={() => { pause(); goto(s.i) }}
                  title={`#${s.seq} ${s.title}${s.summary ? ' — ' + s.summary : ''}`}
                >
                  <span className="tl-idx">{s.seq}</span>
                  <span className="tl-icon">{KIND_ICON[s.kind]}</span>
                  <span className="tl-text">
                    <b>{s.title}</b>
                    {s.summary && <em>{s.summary}</em>}
                  </span>
                  <span className={`tl-chk ${s.check}`}>
                    {s.check === 'ok' ? '✓' : s.check === 'legacy' ? '旧' : s.check === 'mismatch' ? '⚠' : '✕'}
                  </span>
                </button>
              ))}
            </div>
          </aside>
        </div>

        <footer className="replay-controls">
          <button className="mini" onClick={() => { pause(); goto(0) }} disabled={!steps.length} title="回到开局">⏮</button>
          <button className="mini" onClick={() => { pause(); goto(idx - 1) }} disabled={idx <= 0} title="上一步">◀ 上一步</button>
          {playing
            ? <button className="primary" onClick={pause}>⏸ 暂停</button>
            : <button className="primary" onClick={play}>▶ 播放</button>}
          <button className="mini" onClick={() => { pause(); goto(idx + 1) }} disabled={idx >= steps.length - 1} title="下一步">下一步 ▶</button>
          <button className="mini" onClick={() => { pause(); goto(steps.length - 1) }} disabled={!steps.length} title="跳到结尾">⏭</button>

          <input
            className="replay-scrub"
            type="range"
            min={0}
            max={Math.max(0, steps.length - 1)}
            value={idx}
            onChange={(e) => { pause(); goto(Number(e.target.value)) }}
          />
          <span className="replay-counter">{idx + 1} / {steps.length}</span>

          <div className="replay-speed">
            {SPEEDS.map((sp) => (
              <button key={sp} className={`mini ${speed === sp ? 'on' : ''}`} onClick={() => setSpeed(sp)}>
                {sp}×
              </button>
            ))}
          </div>
          <label className="replay-shop-toggle">
            <input type="checkbox" checked={showShop} onChange={(e) => setShowShop(e.target.checked)} />
            显示商店面板
          </label>
        </footer>
        <div className="replay-progress"><span style={{ width: `${progress}%` }} /></div>
      </div>
    </div>
  )
}
