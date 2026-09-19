import Phaser from 'phaser'
import { bus } from './battleBus'

// 纯矢量绘制的战斗场景：不依赖外部贴图。
// React 通过总线驱动：
//   'snapshot' -> 权威快照（空闲时立即生效；播放中暂存，队列播完后校正）
//   'queue'    -> 服务端结算日志（按结算顺序逐条播放动画，播完 emit 'queue_done'）
//   'reset'    -> 清空待播队列
const W = 960
const H = 480
const GROUND_Y = 330
const POS = {
  player: { x: 200, y: GROUND_Y },
  enemy: { x: 760, y: GROUND_Y },
}
const BODY_COLOR = { player: 0x3a7bd5, enemy: 0xe04850 }
const BAR_W = 150

// 状态 id -> 中文名（与后端 STATUS_INFO 对齐，供浮动文字/状态行展示）
export const STATUS_ZH = {
  strength: '力量',
  vulnerable: '易伤',
  fragile: '易碎',
  echo: '回响',
  strength_per_turn: '力量成长',
  power_up: '增伤',
}

const targetKeyOf = (ev) => (ev.target === 'player' ? 'player' : 'enemy')

// ---------- 绘制 ----------
function drawBackdrop(scene) {
  const g = scene.add.graphics().setDepth(0)
  // 夜空
  g.fillGradientStyle(0x1b1b2c, 0x1b1b2c, 0x101018, 0x101018, 1)
  g.fillRect(0, 0, W, H)
  // 星星
  g.fillStyle(0xffffff, 0.45)
  const stars = [[60, 40], [150, 82], [300, 50], [430, 92], [560, 42], [700, 72], [840, 50], [910, 104], [220, 124], [640, 112]]
  stars.forEach(([x, y]) => g.fillCircle(x, y, 1.6))
  // 远山
  g.fillStyle(0x202032, 1)
  g.fillTriangle(-40, GROUND_Y + 40, 180, 150, 400, GROUND_Y + 40)
  g.fillTriangle(300, GROUND_Y + 40, 520, 120, 780, GROUND_Y + 40)
  g.fillTriangle(620, GROUND_Y + 40, 830, 160, 1020, GROUND_Y + 40)
  // 地面
  g.fillStyle(0x242438, 1)
  g.fillRect(0, GROUND_Y + 40, W, H - GROUND_Y - 40)
  g.lineStyle(2, 0x303050, 1)
  g.lineBetween(0, GROUND_Y + 40, W, GROUND_Y + 40)
  // 中央对决标记
  scene.add.text(W / 2, 190, '⚔', { font: '44px sans-serif', fill: '#ffffff' })
    .setOrigin(0.5).setAlpha(0.12).setDepth(0)
}

function drawBody(scene, ent, gray) {
  const { body, radius: r, key } = ent
  const color = gray ? 0x5a5a66 : BODY_COLOR[key]
  const ink = gray ? 0.5 : 1
  body.clear()
  // 地面阴影
  body.fillStyle(0x000000, 0.35)
  body.fillEllipse(0, r + 12, r * 1.7, 14)
  // 身体
  body.fillStyle(color, 1)
  body.fillCircle(0, 0, r)
  body.lineStyle(3, 0x000000, 0.25)
  body.strokeCircle(0, 0, r)
  // 首领尖刺
  if (ent.spikes) {
    body.fillStyle(gray ? 0x4a4a52 : 0x8a2f36, 1)
    for (let i = -1; i <= 1; i += 1) {
      body.fillTriangle(i * r * 0.52 - 9, -r + 8, i * r * 0.52 + 9, -r + 8, i * r * 0.52, -r - 18)
    }
  }
  // 眼睛
  const eyeY = -r * 0.18
  const eyeDX = r * 0.36
  const eyeR = r * 0.16
  body.fillStyle(0xffffff, ink)
  body.fillCircle(-eyeDX, eyeY, eyeR)
  body.fillCircle(eyeDX, eyeY, eyeR)
  body.fillStyle(0x1a1a24, ink)
  body.fillCircle(-eyeDX, eyeY, eyeR * 0.5)
  body.fillCircle(eyeDX, eyeY, eyeR * 0.5)
  if (key === 'enemy') {
    // 怒眉
    body.lineStyle(3, 0x1a1a24, ink)
    body.lineBetween(-eyeDX - eyeR, eyeY - eyeR - 4, -eyeDX + eyeR, eyeY - eyeR + 3)
    body.lineBetween(eyeDX + eyeR, eyeY - eyeR - 4, eyeDX - eyeR, eyeY - eyeR + 3)
  } else {
    // 微笑
    body.lineStyle(3, 0x1a1a24, gray ? 0.5 : 0.75)
    body.beginPath()
    body.arc(0, r * 0.1, r * 0.4, Phaser.Math.DegToRad(25), Phaser.Math.DegToRad(155), false)
    body.strokePath()
  }
}

function buildEntity(scene, key) {
  const radius = key === 'player' ? 40 : 44
  const container = scene.add.container(POS[key].x, POS[key].y).setDepth(2)
  const bar = scene.add.graphics()
  const body = scene.add.graphics()
  const shield = scene.add.graphics()
  const nameText = scene.add.text(0, 0, '', { font: '600 15px sans-serif', fill: '#fff' }).setOrigin(0.5)
  const statusText = scene.add.text(0, 0, '', { font: '12px sans-serif', fill: '#fc6' }).setOrigin(0.5)
  const hpText = scene.add.text(0, 0, '', { font: '11px sans-serif', fill: '#fff' }).setOrigin(0.5)
  const blockText = scene.add.text(0, 0, '', { font: '700 14px sans-serif', fill: '#9cf' }).setOrigin(0.5)
  container.add([bar, body, shield, nameText, statusText, hpText, blockText])
  const ent = {
    key, container, bar, body, shield, nameText, statusText, hpText, blockText,
    radius, spikes: false, dead: false, idleTween: null, floatIdx: 0,
  }
  drawBody(scene, ent, false)
  return ent
}

// ---------- 场景 ----------
export default class BattleScene extends Phaser.Scene {
  constructor() {
    super('battle')
  }

  create() {
    this._queue = []
    this._playing = false
    this._pendingSnap = null
    this.enemyId = null

    drawBackdrop(this)
    this.entities = {
      player: buildEntity(this, 'player'),
      enemy: buildEntity(this, 'enemy'),
    }
    this.display = { player: null, enemy: null }

    this.turnText = this.add.text(16, 12, '', { font: '13px sans-serif', fill: '#889' }).setDepth(10)
    this.bannerText = this.add.text(W / 2, 92, '', {
      font: '700 26px sans-serif', fill: '#ffd166', stroke: '#000', strokeThickness: 4,
    }).setOrigin(0.5).setDepth(10).setAlpha(0)

    this._handlers = [
      bus.on('snapshot', (s) => this.onSnapshot(s)),
      bus.on('queue', (entries) => this.enqueue(entries)),
      bus.on('reset', () => this.resetScene()),
    ]
    this.events.once('shutdown', () => {
      if (this._handlers) this._handlers.forEach((off) => off())
      this._handlers = []
    })

    // 占位快照：React 收到 phaser_ready 后会立即补发真实快照（续局恢复也走这条路）
    this.applySnapshot({
      player: { name: '勇者', hp: 1, max_hp: 1, block: 0, alive: true, statuses: [] },
      enemy: { name: '敌人', hp: 1, max_hp: 1, block: 0, alive: true, statuses: [] },
    })
    bus.emit('phaser_ready')
  }

  // ---------- 快照（权威状态） ----------
  onSnapshot(s) {
    if (this._playing) {
      // 播放期间不插队：队列播完后统一校正到权威状态
      this._pendingSnap = s
    } else {
      this.applySnapshot(s)
    }
  }

  applySnapshot(s) {
    if (!s) return
    for (const key of ['player', 'enemy']) {
      const d = s[key]
      if (!d) continue
      if (key === 'enemy' && s.enemy_id && s.enemy_id !== this.enemyId) {
        this.enemyId = s.enemy_id
        const ent = this.entities.enemy
        const boss = String(s.enemy_id).startsWith('boss')
        ent.radius = boss ? 60 : 44
        ent.spikes = boss
      }
      const hp = d.hp ?? 0
      const alive = d.alive !== false && hp > 0
      this.display[key] = {
        name: d.name || (key === 'player' ? '勇者' : '敌人'),
        hp,
        max_hp: d.max_hp ?? 1,
        block: d.block ?? 0,
        alive,
        statuses: Array.isArray(d.statuses) ? d.statuses : [],
      }
      // 续局/刷新恢复：直接落到对应姿态，不播动画
      if (alive) this.setAlivePose(key)
      else this.setDeadPose(key)
      this.renderEntity(key)
    }
    if (typeof s.turn === 'number') this.turnText.setText(`回合 ${s.turn}`)
  }

  setAlivePose(key) {
    const ent = this.entities[key]
    if (!ent.dead && ent.idleTween) return // 已是待机
    ent.dead = false
    if (ent.idleTween) ent.idleTween.stop()
    ent.container.setPosition(POS[key].x, POS[key].y)
    ent.container.setAlpha(1).setAngle(0)
    drawBody(this, ent, false)
    // 待机：缓慢上下浮动
    ent.idleTween = this.tweens.add({
      targets: ent.container,
      y: POS[key].y - 10,
      duration: key === 'player' ? 1300 : 1500,
      yoyo: true,
      repeat: -1,
      ease: 'Sine.easeInOut',
    })
  }

  setDeadPose(key) {
    const ent = this.entities[key]
    if (ent.idleTween) ent.idleTween.stop()
    ent.idleTween = null
    ent.dead = true
    drawBody(this, ent, true)
    ent.container.setPosition(POS[key].x, POS[key].y + 26)
    ent.container.setAlpha(0.3).setAngle(key === 'player' ? -10 : 10)
  }

  renderEntity(key) {
    const ent = this.entities[key]
    const d = this.display[key]
    if (!ent || !d) return
    const r = ent.radius
    ent.nameText.setText(d.name).setPosition(0, -r - 42)
    ent.statusText.setText(d.statuses.map((s) => `${s.name}${s.value}`).join('  ')).setPosition(0, -r - 24)
    // 血条
    const pct = Math.max(0, Math.min(1, d.hp / (d.max_hp || 1)))
    ent.bar.clear()
    ent.bar.fillStyle(0x101018, 1).fillRect(-BAR_W / 2 - 1, r + 14, BAR_W + 2, 13)
    if (pct > 0) {
      ent.bar.fillStyle(key === 'player' ? 0x53c66e : 0xe04850, 1).fillRect(-BAR_W / 2, r + 15, BAR_W * pct, 11)
    }
    ent.hpText.setText(`${d.hp} / ${d.max_hp}`).setPosition(0, r + 20)
    // 护盾
    ent.shield.clear()
    if (d.block > 0) {
      ent.shield.lineStyle(3, 0x88c0ff, 0.85)
      ent.shield.strokeCircle(0, 0, r + 7)
      ent.blockText.setText(`🛡${d.block}`).setPosition(r + 26, -r - 6)
    } else {
      ent.blockText.setText('')
    }
  }

  resetScene() {
    this._queue.length = 0
    this._pendingSnap = null
  }

  // ---------- 结算队列：按服务端顺序逐条播放 ----------
  enqueue(entries) {
    if (Array.isArray(entries)) this._queue.push(...entries)
    if (!this._playing) this._pump()
  }

  async _pump() {
    this._playing = true
    while (this._queue.length) {
      const entry = this._queue.shift()
      try {
        await this.playEntry(entry)
      } catch (e) {
        // 单条动画异常不阻塞后续结算
      }
    }
    this._playing = false
    if (this._pendingSnap) {
      const s = this._pendingSnap
      this._pendingSnap = null
      this.applySnapshot(s)
    }
    bus.emit('queue_done')
  }

  async playEntry(entry) {
    if (!entry || typeof entry !== 'object') return
    // 战斗结果（死亡动画 + 横幅），附带权威快照
    if (entry.result) {
      if (entry.result === 'won' || entry.result === 'run_won') {
        await this.die(this.entities.enemy)
        await this.banner(entry.result === 'run_won' ? '🏆 通关！' : '胜利！', '#ffd166', 650)
      } else if (entry.result === 'lost') {
        await this.die(this.entities.player)
        await this.banner('战败…', '#ff6b6b', 650)
      }
      if (entry.snapshot) this.applySnapshot(entry.snapshot)
      return
    }
    // 权威快照校正点
    if (entry.snapshot) {
      this.applySnapshot(entry.snapshot)
      await this.pause(80)
      return
    }
    const ev = entry
    switch (ev.action) {
      case 'enemy_turn':
        await this.banner(`敌方回合${ev.extra?.name ? ` · ${ev.extra.name}` : ''}`, '#ff9f43', 380)
        break
      case 'damage':
      case 'echo_damage':
        await this.onDamage(ev)
        break
      case 'gain_block':
        await this.onBlock(ev)
        break
      case 'heal':
        await this.onHeal(ev)
        break
      case 'apply_status':
      case 'set_status':
        await this.onStatus(ev)
        break
      case 'draw':
        this.floatText(POS.player.x, POS.player.y - 120, `抽牌 +${ev.value}`, '#9cf')
        await this.pause(160)
        break
      case 'gain_energy':
        this.floatText(POS.player.x, POS.player.y - 120, `能量 +${ev.value}`, '#ffd166')
        await this.pause(160)
        break
      case 'truncated':
        await this.banner('⚠ 连锁达到上限，强制终止', '#ff6b6b', 500)
        break
      default:
        break
    }
  }

  // ---------- 各效果动画 ----------
  async onDamage(ev) {
    const tKey = targetKeyOf(ev)
    const tgt = this.entities[tKey]
    const src = this.entities[ev.source]
    const impact = () => {
      this.impact(tgt)
      this.applyDamage(tKey, ev.value || 0)
    }
    if (src && src.key !== tKey && !src.dead) {
      // 攻击：前冲 -> 命中 -> 回位
      await this.lunge(src, tgt, impact)
    } else {
      impact()
    }
    const d = this.display[tKey]
    if (d && d.hp <= 0) {
      await this.die(tgt) // 死亡打断：连锁在死亡处停顿
    } else {
      await this.pause(140)
    }
  }

  async onBlock(ev) {
    const key = targetKeyOf(ev)
    const d = this.display[key]
    const ent = this.entities[key]
    if (d) {
      d.block = (d.block || 0) + (ev.value || 0)
      this.renderEntity(key)
    }
    this.floatOn(ent, `格挡 +${ev.value}`, '#88c0ff')
    this.tweens.add({ targets: ent.shield, scaleX: 1.3, scaleY: 1.3, duration: 130, yoyo: true, ease: 'Quad.easeOut' })
    await this.pause(240)
  }

  async onHeal(ev) {
    const key = targetKeyOf(ev)
    const d = this.display[key]
    const ent = this.entities[key]
    if (d) {
      d.hp = Math.min(d.max_hp, d.hp + (ev.value || 0))
      this.renderEntity(key)
    }
    this.floatOn(ent, `+${ev.value} 生命`, '#5f8')
    const glow = this.add.circle(ent.container.x, ent.container.y, ent.radius + 4, 0x55ff88, 0.35).setDepth(1)
    this.tweens.add({
      targets: glow, alpha: 0, scaleX: 1.4, scaleY: 1.4, duration: 350,
      onComplete: () => glow.destroy(),
    })
    await this.pause(240)
  }

  async onStatus(ev) {
    const key = targetKeyOf(ev)
    const d = this.display[key]
    const ent = this.entities[key]
    const sid = ev.extra?.status || ''
    const name = STATUS_ZH[sid] || sid
    if (d) {
      const list = d.statuses
      const idx = list.findIndex((s) => s.id === sid)
      if (ev.action === 'set_status' || idx < 0) {
        const next = { id: sid, name, value: ev.value || 0 }
        if (idx >= 0) list[idx] = next
        else list.push(next)
      } else {
        list[idx].value += ev.value || 0
      }
      this.renderEntity(key)
    }
    this.floatOn(ent, `${name} ${ev.value > 0 ? '+' : ''}${ev.value}`, '#fc6')
    await this.pause(260)
  }

  // 展示层血量/护盾推算（与服务端 _hurt 一致；最终以快照校正为准）
  applyDamage(key, dmg) {
    const d = this.display[key]
    if (!d) return
    const absorbed = Math.min(d.block || 0, dmg)
    d.block = Math.max(0, (d.block || 0) - absorbed)
    const hpLoss = Math.max(0, dmg - absorbed)
    d.hp = Math.max(0, d.hp - hpLoss)
    const ent = this.entities[key]
    if (absorbed > 0) this.floatOn(ent, `格挡 -${absorbed}`, '#88c0ff')
    if (hpLoss > 0) this.floatOn(ent, `-${hpLoss}`, '#ff5b5b')
    if (hpLoss <= 0 && absorbed > 0) this.floatOn(ent, '完全格挡', '#88c0ff')
    this.renderEntity(key)
  }

  // ---------- 动画原语 ----------
  lunge(ent, target, onImpact) {
    return new Promise((resolve) => {
      const home = POS[ent.key].x
      const dir = Math.sign(target.container.x - home) || 1
      const reach = Math.max(60, Math.abs(target.container.x - home) - target.radius - ent.radius - 30)
      this.tweens.add({
        targets: ent.container,
        x: home + dir * reach,
        duration: 150,
        ease: 'Quad.easeOut',
        onComplete: () => {
          if (onImpact) onImpact()
          this.tweens.add({
            targets: ent.container, x: home, duration: 220, ease: 'Quad.easeIn',
            onComplete: resolve,
          })
        },
      })
    })
  }

  impact(ent) {
    const home = POS[ent.key].x
    this.cameras.main.shake(70, 0.0035)
    // 受击：抖动 + 白闪
    this.tweens.add({
      targets: ent.container, x: home + 7, duration: 45, yoyo: true, repeat: 3,
      onComplete: () => { if (!ent.dead) ent.container.x = home },
    })
    const flash = this.add.circle(ent.container.x, ent.container.y, ent.radius + 8, 0xffffff, 0.5).setDepth(4)
    this.tweens.add({ targets: flash, alpha: 0, duration: 200, onComplete: () => flash.destroy() })
  }

  die(ent) {
    if (!ent || ent.dead) return Promise.resolve()
    ent.dead = true
    if (ent.idleTween) ent.idleTween.stop()
    ent.idleTween = null
    drawBody(this, ent, true)
    // 死亡：倒地、变灰、淡出
    return new Promise((resolve) => {
      this.tweens.add({
        targets: ent.container,
        y: POS[ent.key].y + 26,
        alpha: 0.3,
        angle: ent.key === 'player' ? -10 : 10,
        duration: 550,
        ease: 'Quad.easeIn',
        onComplete: resolve,
      })
    })
  }

  banner(text, color = '#ffd166', hold = 420) {
    return new Promise((resolve) => {
      this.bannerText.setText(text).setColor(color)
      this.tweens.add({
        targets: this.bannerText, alpha: 1, duration: 120,
        onComplete: () => {
          this.time.delayedCall(hold, () => {
            this.tweens.add({
              targets: this.bannerText, alpha: 0, duration: 160,
              onComplete: resolve,
            })
          })
        },
      })
    })
  }

  floatOn(ent, text, css) {
    ent.floatIdx = (ent.floatIdx || 0) + 1
    const offY = (ent.floatIdx % 3) * 20
    this.floatText(ent.container.x, ent.container.y - ent.radius - 26 - offY, text, css)
  }

  floatText(x, y, text, css) {
    const t = this.add.text(x, y, text, {
      font: '700 18px sans-serif', fill: css, stroke: '#000', strokeThickness: 3,
    }).setOrigin(0.5).setDepth(6)
    this.tweens.add({
      targets: t, y: y - 44, alpha: 0, duration: 850, ease: 'Quad.easeOut',
      onComplete: () => t.destroy(),
    })
  }

  pause(ms) {
    return new Promise((resolve) => this.time.delayedCall(ms, resolve))
  }
}

export function startPhaser(container) {
  const game = new Phaser.Game({
    type: Phaser.AUTO,
    parent: container,
    width: W,
    height: H,
    backgroundColor: '#1a1a24',
    scene: BattleScene,
  })
  return game
}
