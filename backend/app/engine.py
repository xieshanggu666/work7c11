from __future__ import annotations

import math

from .settlement import EffectEvent, SettlementQueue

# 状态 id -> 中文名/说明（供前端展示）
STATUS_INFO = {
    "strength": "力量",
    "vulnerable": "易伤",
    "fragile": "易碎",
    "echo": "回响",
    "strength_per_turn": "力量成长",
    "power_up": "增伤",
}


def make_entity(key, name, max_hp, hp=None):
    return {
        "key": key, "name": name, "max_hp": max_hp, "hp": hp if hp is not None else max_hp,
        "block": 0, "statuses": {}, "alive": True,
    }


class Battle:
    """单场战斗：玩家 vs 一个敌人/首领。resolve 消费结算队列，产出事件日志。"""

    def __init__(self, run_state, enemy_def, seed, battle_index, relic_status="", relic_power=0, boss_hp_bonus=0,
                 card_instances=None):
        max_hp = run_state["max_health"]
        self.entities = {
            "player": make_entity("player", run_state.get("player_name", "勇者"), max_hp, run_state["health"]),
        }
        if boss_hp_bonus:
            enemy_hp = enemy_def["hp"] + boss_hp_bonus
        else:
            enemy_hp = enemy_def["hp"]
        enemy_key = "enemy"
        self.enemy_def = enemy_def
        self.enemy = make_entity(enemy_key, enemy_def["name"], enemy_hp, enemy_hp)
        self.entities[enemy_key] = self.enemy

        # 由奖励选择带入的常驻效果（relic）幻化成战斗初始状态
        st = self.entities["player"]["statuses"]
        if relic_status:
            st[relic_status] = {"mode": "add", "value": relic_power, "ticks": None}
        if "power_up" in run_state.get("relics", {}):
            st.setdefault("strength", {"mode": "add", "value": 0, "ticks": None})["value"] += 2

        # 敌人阶段
        self.phase = 0
        self.phases_active = False

        # 牌堆（确定性：用 run_seed + battle_index 洗牌）
        # run_state["deck"] 为引用列表：新档是实例 uid，旧档是卡牌 id（兼容）
        self.seed = seed
        self.battle_index = battle_index
        deck = list(run_state["deck"])
        rng = _seeded_rng(seed, battle_index)
        rng.shuffle(deck)
        self.draw_pile = deck
        self.hand = []
        self.discard = []
        # uid -> 卡牌实例 {"id","forges"}；旧档（裸 id 牌堆）为空 dict
        self.card_instances = dict(card_instances or {})
        self.energy = run_state.get("base_energy", 3)
        self.max_energy = run_state.get("base_energy", 3)
        self.in_turn = False
        self.turn = 0
        self.truncated = False
        self.queue = SettlementQueue(self)

    # ---------- 卡牌实例 ----------
    def _card_def(self, ref):
        """手牌引用（uid 或旧档裸 id）-> 生效卡牌定义（含锻造换算）。"""
        from .cards import get_card
        from .forging import effective_card
        inst = self.card_instances.get(ref)
        if inst is None:
            return get_card(ref)
        return effective_card(get_card(inst["id"]), inst.get("forges", []))

    # ---------- 实体查询 ----------
    def entity(self, key):
        return self.entities.get(key)

    def is_dead(self, key):
        ent = self.entities.get(key)
        return ent is None or not ent["alive"]

    def hpof(self, key):
        ent = self.entity(key)
        return ent["hp"] if ent else 0

    # ---------- 手感用 ----------
    def player_attack_base(self):
        p = self.entities["player"]
        return 0

    # ---------- 效果解析（返回子事件 = 连锁） ----------
    def resolve(self, ev: EffectEvent) -> list:
        children = []
        a = ev.action
        target = self.entities.get(ev.target)

        if a == "damage" or a == "echo_damage":
            if target is None or not target["alive"]:
                return children
            dmg = _final_damage(self, ev)
            _hurt(target, dmg)
            ev.value = dmg  # 日志记录实际结算伤害
            # 连锁：攻击者的回响 -> 再攻击
            if "attack" in ev.tags:
                src = self.entities.get(ev.source)
                if src and src["alive"] and src["statuses"].get("echo"):
                    ech = src["statuses"]["echo"]["value"]
                    if ech > 0:
                        src["statuses"]["echo"]["value"] = ech - 1
                        children.append(EffectEvent(
                            "damage", target=ev.target, value=max(1, int(ev.value / 2)),
                            source=ev.source, tags=["attack", "echo"]))
        elif a == "gain_block":
            if target and target["alive"]:
                target["block"] += ev.value
        elif a == "draw":
            # 抽出 draw_pile 前 value 张进手牌
            for _ in range(ev.value):
                if not self.draw_pile:
                    self.draw_pile = list(self.discard); self.discard = []
                    rng = _seeded_rng(self.seed, self.battle_index + self.turn + 99)
                    rng.shuffle(self.draw_pile)
                    if not self.draw_pile:
                        break
                self.hand.append(self.draw_pile.pop(0))
        elif a == "apply_status":
            if target and target["alive"]:
                _apply_status(target, ev.extra.get("status"), ev.value,
                              ev.extra.get("stack", "add"), ev.extra.get("ticks"))
        elif a == "set_status":
            if target and target["alive"]:
                target["statuses"][ev.extra["status"]] = {
                    "mode": ev.extra.get("stack", "replace"),
                    "value": ev.value, "ticks": ev.extra.get("ticks"),
                }
        elif a == "heal":
            if target and target["alive"]:
                target["hp"] = min(target["max_hp"], target["hp"] + ev.value)
        elif a == "gain_energy":
            self.energy += ev.value
        return children

    # ---------- 死亡结算与连锁中断 ----------
    def collect_deaths(self, queue: SettlementQueue):
        changed = True
        while changed:
            changed = False
            for key, ent in self.entities.items():
                if ent["alive"] and ent["hp"] <= 0:
                    ent["alive"] = False
                    ent["hp"] = 0
                    changed = True
                    # 死亡打断：取消所有仍指向它的待处理事件（阻止后续连锁作用死目标）
                    queue.pending = [e for e in queue.pending if e.target != key]
                    continue

    # ---------- 回合流程 ----------
    def start_turn(self):
        self.turn += 1
        self.in_turn = True
        self.energy = self.max_energy
        p = self.entities["player"]
        p["block"] = 0  # 玩家格挡回合末清空（简化）
        # 回合开始触发：力量成长
        spt = p["statuses"].get("strength_per_turn")
        if spt:
            _apply_status(p, "strength", spt["value"], "add")
        # 抽牌 & 韧性递减
        have_draw = 0
        for _ in range(5):
            if not self.draw_pile:
                self.draw_pile = list(self.discard); self.discard = []
                rng = _seeded_rng(self.seed, self.battle_index * 1000 + self.turn)
                rng.shuffle(self.draw_pile)
            if not self.draw_pile:
                break
            self.hand.append(self.draw_pile.pop(0)); have_draw += 1
        for ent in self.entities.values():
            _tick_statuses(ent)
        return self.to_snapshot()

    def end_turn(self):
        """敌方行动并推进回合。返回 (敌方结算日志, 意图)，日志按结算顺序供前端播放。"""
        p = self.entities["player"]
        p["block"] = 0
        logs, intent = [], None
        # 敌人行动
        if self.enemy["alive"]:
            intent = self._enemy_intent()
            q = SettlementQueue(self)
            for eff in intent["effects"]:
                t = eff["type"]
                # 与 play_card 对称的默认目标：伤害指向玩家，其余默认作用于敌方自身
                target = eff.get("target") or ("player" if t in ("damage", "echo_damage") else "enemy")
                q.push(EffectEvent(t, target=target, value=eff.get("value", 0),
                                   source="enemy", tags=eff.get("tags", []),
                                   extra={"status": eff.get("status"), "ticks": eff.get("ticks"),
                                          "stack": eff.get("stack")}))
            logs = self._run_sub(q, intent)
        self.collect_deaths(self.queue)
        # 战斗未结束则进入玩家下一回合（重新获得能量并抽牌）
        if self.battle_result() == "ongoing":
            self.start_turn()
        return logs, intent

    def _enemy_intent(self):
        """确定性选择敌人意图（返回完整技能：名称 + 全部效果）。"""
        en = self.enemy_def
        if en.get("phases"):
            # 按阶段选技能列表
            phase = self.phase
            if en["boss"] and self.turn % 4 == 0:
                phase = min(phase + 1, len(en["phases"]) - 1)
                self.phase = phase
            skills = en["phases"][phase]["skills"]
        else:
            skills = en["skills"]
        rng = _seeded_rng(self.seed, self.battle_index * 100000 + self.turn)
        skill = skills[rng.randrange(len(skills))]
        return {"name": skill.get("name", ""), "effects": skill["effects"]}

    def _run_sub(self, q, intent=None):
        log = q.run()
        self.truncated = self.truncated or q.truncated
        return log

    def play_card(self, card_ref_or_def):
        """玩家打出一张手牌，走结算队列。返回过程日志。

        card_ref_or_def 兼容两种形态：
        - 新档：手牌引用（uid 字符串），通过 _card_def 解析生效卡牌；
        - 旧档/单测：直接传入卡牌定义 dict（含 id 与 effects）。
        """
        if not self.in_turn:
            raise ValueError("当前不是玩家回合")
        if isinstance(card_ref_or_def, dict):
            card_ref = card_ref_or_def["id"]
            c = card_ref_or_def
        else:
            card_ref = card_ref_or_def
            c = self._card_def(card_ref)
        # 打出手牌：从 hand 移除（能量校验由 service 层完成）
        if card_ref not in self.hand:
            raise ValueError("手牌中不存在该卡牌")
        self.hand.remove(card_ref)
        self.discard.append(card_ref)
        q = SettlementQueue(self)
        for eff in c["effects"]:
            t = eff["type"]
            if t in ("apply_status", "set_status", "gain_block", "heal", "draw", "gain_energy"):
                target = eff.get("target", "player")
            else:
                target = eff.get("target", "enemy")
            q.push(EffectEvent(
                eff["type"], target=target, value=eff.get("value", 0),
                source="player", tags=eff.get("tags", []),
                extra={"status": eff.get("status"), "ticks": eff.get("ticks"),
                       "stack": eff.get("stack")}))
        log = q.run()
        self.truncated = self.truncated or q.truncated
        return log

    # ---------- 序列化（续局持久化） ----------
    def dump(self):
        ents = {}
        for k, ent in self.entities.items():
            ents[k] = {
                "name": ent["name"], "hp": ent["hp"], "max_hp": ent["max_hp"],
                "block": ent["block"], "alive": ent["alive"], "statuses": ent["statuses"],
            }
        return {
            "index": self.battle_index,
            "enemy": self.enemy_def["id"],
            "entities": ents,
            "draw_pile": list(self.draw_pile), "hand": list(self.hand),
            "discard": list(self.discard),
            "card_instances": dict(self.card_instances),
            "energy": self.energy, "max_energy": self.max_energy,
            "turn": self.turn, "in_turn": self.in_turn, "phase": self.phase,
            "truncated": self.truncated,
        }

    @classmethod
    def from_state(cls, bstate, enemy_def, seed, health=75, max_health=75, relic_status="", relic_power=0):
        b = cls.__new__(cls)
        b.enemy_def = enemy_def
        b.seed = seed
        b.battle_index = bstate.get("index", 0)
        b.entities = {}
        for k, ed in bstate.get("entities", {}).items():
            b.entities[k] = {
                "key": k, "name": ed["name"], "max_hp": ed["max_hp"],
                "hp": ed["hp"], "block": ed["block"], "alive": ed["alive"],
                "statuses": ed.get("statuses", {}),
            }
        b.enemy = b.entities.get("enemy") or next((v for v in b.entities.values() if v["key"] == "enemy"), None)
        b.draw_pile = list(bstate.get("draw_pile", []))
        b.hand = list(bstate.get("hand", []))
        b.discard = list(bstate.get("discard", []))
        b.card_instances = dict(bstate.get("card_instances", {}))
        b.energy = bstate.get("energy", 3)
        b.max_energy = bstate.get("max_energy", 3)
        b.turn = bstate.get("turn", 0)
        b.in_turn = bstate.get("in_turn", False)
        b.phase = bstate.get("phase", 0)
        b.truncated = bstate.get("truncated", False)
        b.queue = SettlementQueue(b)
        return b

    # ---------- 快照 ----------
    def to_snapshot(self):
        ents = {}
        for k, ent in self.entities.items():
            ents[k] = {
                "name": ent["name"], "hp": ent["hp"], "max_hp": ent["max_hp"],
                "block": ent["block"], "alive": ent["alive"],
                "statuses": _statuses_public(ent["statuses"]),
            }
        return {
            "player": ents.get("player"), "enemy": ents.get("enemy"),
            "energy": self.energy, "max_energy": self.max_energy,
            "turn": self.turn, "in_turn": self.in_turn, "truncated": self.truncated,
            "hand": list(self.hand),
        }

    def battle_result(self):
        p = self.entities["player"]
        e = self.enemy
        if p["alive"] and e["alive"]:
            return "ongoing"
        if not e["alive"]:
            return "won"
        if not p["alive"]:
            return "lost"
        return "ongoing"


# ---------- 辅助 ----------
def _seeded_rng(seed, salt):
    import random
    return random.Random((seed * 1000003 + salt) & 0x7fffffff)


def _final_damage(battle, ev):
    dmg = float(ev.value)
    target = battle.entities.get(ev.target)
    src = battle.entities.get(ev.source)
    if src and src.get("statuses", {}).get("strength") and "attack" in (ev.tags or ()):
        dmg += src["statuses"]["strength"]["value"]
    if target and target.get("statuses", {}).get("vulnerable"):
        dmg *= 1.5
    if target and target.get("statuses", {}).get("fragile"):
        dmg *= 1.5
    return max(0, int(round(dmg)))


def _hurt(ent, dmg):
    rem = dmg
    if ent["block"] > 0:
        absorbed = min(ent["block"], rem)
        ent["block"] -= absorbed
        rem -= absorbed
    if rem > 0:
        ent["hp"] = max(0, ent["hp"] - rem)


def _apply_status(ent, sid, value, mode="add", ticks=None):
    st = ent["statuses"]
    cur = st.get(sid)
    if mode == "add":
        if cur:
            cur["value"] += value
        else:
            st[sid] = {"mode": "add", "value": value, "ticks": ticks}
    elif mode == "replace":
        st[sid] = {"mode": "replace", "value": value, "ticks": ticks}
    else:
        st[sid] = {"mode": mode, "value": value, "ticks": ticks}


def _tick_statuses(ent):
    # 韧性随回合衰减；永久状态（ticks=None）不衰减
    keep = {}
    for sid, s in ent["statuses"].items():
        if s.get("ticks") is not None:
            if s["ticks"] > 1:
                keep[sid] = {**s, "ticks": s["ticks"] - 1}
            else:
                continue
        else:
            keep[sid] = s
    ent["statuses"] = keep


def _statuses_public(statuses):
    out = []
    for sid, s in statuses.items():
        out.append({"id": sid, "name": STATUS_INFO.get(sid, sid),
                    "value": s["value"], "mode": s["mode"]})
    return out