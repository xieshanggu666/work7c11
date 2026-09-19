from __future__ import annotations

import random

from .cards import get_card

# 战斗胜利奖励：给 2-3 个“卡牌/回血/金币”选项，选一个。
# 每个选项带 effects：改变 run 状态（加牌/回血/金、或引入后续遭遇的隐性修正 relic）。

ALL_RELIC_OPTIONS = [
    {"id": "anvil", "name": "铁砧", "desc": "获得永久力量 +2（影响后续所有战斗）",
     "effects": [{"type": "relic_set", "relic": "power_up", "value": 1}]},
    {"id": "curse_core", "name": "诅咒之核", "desc": "本局增伤 +25%，但首领生命 +20%（后续遭遇更强）",
     "effects": [{"type": "relic_set", "relic": "boss_hp_bonus", "value": 12},
                 {"type": "relic_set", "relic": "power_up", "value": 1}]},
    {"id": "heal_15", "name": "治疗药膏", "desc": "回复 15 点生命", "effects": [{"type": "heal_run", "value": 15}]},
    {"id": "card_flurry", "name": "『连击』", "desc": "把「连击」加入牌组", "effects": [{"type": "add_card", "card": "flurry"}]},
    {"id": "gold_30", "name": "金币袋", "desc": "获得 30 金币", "effects": [{"type": "gold", "value": 30}]},
]


def battle_reward_options(rng_seed, enemy_def, run_state):
    """根据敌人掉落卡与金币生成 2-3 个可直接领取的奖励选项（确定性）。"""
    rng = random.Random((rng_seed * 31 + 7) & 0xFFFFFFFF)
    reward_cards = enemy_def.get("reward_cards") or []
    options = []
    seen = set()

    def add(kind, name, desc, effects, key=None):
        key = key or kind
        if key in seen:
            return
        seen.add(key)
        options.append({"kind": kind, "name": name, "desc": desc, "effects": effects})

    # 掉落卡：确定抽一张加入牌组（同名卡已持有则不再提供该选项）
    if reward_cards:
        cid = reward_cards[rng.randrange(len(reward_cards))]
        c = get_card(cid)
        instances = run_state.get("card_instances")
        if instances:
            owned = {inst["id"] for inst in instances.values()}
        else:
            owned = set(run_state.get("deck", []))  # 旧档：裸 id 牌组
        if cid not in owned:
            add("card", c["name"], f"把「{c['name']}」加入牌组",
                [{"type": "add_card", "card": cid}], key="card")
    # 金币
    add("gold", "金币袋", f"获得 {enemy_def.get('reward_gold', 30)} 金币",
        [{"type": "gold", "value": enemy_def.get("reward_gold", 30)}], key="gold")
    # 治疗
    add("heal", "药膏", "回复 12 点生命", [{"type": "heal_run", "value": 12}], key="heal")
    # 遗物（直接给一个真实遗物，起训练用）
    relic = rng.choice([o for o in ALL_RELIC_OPTIONS])
    add("relic", relic["name"], relic["desc"], relic["effects"], key="relic")
    return options


def relic_choice_options(rng_seed):
    rng = random.Random((rng_seed * 53 + 3) & 0xFFFFFFFF)
    pool = [o for o in ALL_RELIC_OPTIONS]
    return rng.sample(pool, k=min(2, len(pool)))