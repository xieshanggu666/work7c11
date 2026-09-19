from __future__ import annotations

# 卡牌注册表（数据驱动）。效果用声明式 dict 表示，由 settlement 引擎解释。
# 每个效果至多带 2 个扩展字段（title/desc/trigger），由前端渲染与引擎事件订阅使用。

CARDS: dict[str, dict] = {}


def _card(cid, name, cost, ctype, tier, effects, desc, unlock_of=None, tags=()):
    CARDS[cid] = {
        "id": cid, "name": name, "cost": cost, "type": ctype, "tier": tier,
        "effects": effects, "desc": desc, "unlock_of": unlock_of or cid,
        "tags": list(tags),
    }


# ---------------- 基础携带牌（初始牌组） ----------------
_card("strike", "打击", 1, "attack", "basic", [{"type": "damage", "value": 6, "target": "enemy", "tags": ["attack"]}],
      "造成 6 点伤害。", tags=["攻击"])
_card("guard", "防御", 1, "skill", "basic", [{"type": "gain_block", "value": 5, "target": "player"}],
      "获得 5 点格挡。")

# ---------------- 进阶（tier 随失败解锁） ----------------
_card("heavy_blow", "重击", 2, "attack", "advanced", [{"type": "damage", "value": 14, "target": "enemy", "tags": ["attack"]}],
      "造成 14 点伤害。")
_card("cleave", "顺劈", 2, "attack", "advanced", [{"type": "damage", "value": 9, "target": "enemy", "tags": ["attack"]}],
      "造成 9 点伤害，触发连环。（当敌人携带缠斗触发型状态时的连锁演示）")
_card("shield_bash", "盾击", 2, "attack", "advanced",
      [{"type": "gain_block", "value": 4, "target": "player"},
       {"type": "damage", "value": 5, "target": "enemy", "tags": ["attack"]}],
      "获得 4 点格挡，造成 5 点伤害。")
_card("pommel", "剑柄打击", 1, "attack", "advanced",
      [{"type": "damage", "value": 8, "target": "enemy", "tags": ["attack"]},
       {"type": "draw", "value": 1}],
      "造成 8 点伤害，抽 1 张牌。")
_card("battle_trance", "战吼", 0, "skill", "advanced", [{"type": "draw", "value": 3}],
      "抽 3 张牌。")
_card("flex", "暴怒", 0, "skill", "advanced",
      [{"type": "apply_status", "status": "strength", "value": 2, "stack": "add"}],
      "获得 2 点力量。")
_card("iron_wave", "铁波", 1, "attack", "advanced",
      [{"type": "gain_block", "value": 5, "target": "player"},
       {"type": "damage", "value": 5, "target": "enemy", "tags": ["attack"]}],
      "获得 5 点格挡，造成 5 点伤害。")

# ---------------- 金色（连锁/连击核心） ----------------
_card("flurry", "连击", 1, "attack", "gold",
      [{"type": "damage", "value": 4, "target": "enemy", "tags": ["attack"]},
       {"type": "apply_status", "status": "vulnerable", "value": 1, "stack": "add", "target": "enemy",
        "ticks": 2}],
      "造成 4 点伤害，施加易伤 2 回合。易伤触发的连锁每次攻击额外伤害。")
_card("adrenaline", "肾上腺素", 0, "skill", "gold", [{"type": "draw", "value": 2}],
      "抽 2 张牌。")
_card("swift", "疾风连击", 1, "attack", "gold",
      [{"type": "damage", "value": 3, "target": "enemy", "tags": ["attack"]},
       {"type": "damage", "value": 3, "target": "enemy", "tags": ["attack"]}],
      "造成 3 点伤害两次（两次独立结算，便于验证连锁触发）。")
_card("blood_echo", "血之回响", 1, "attack", "gold",
      [{"type": "apply_status", "status": "echo", "value": 1, "stack": "add"},
       {"type": "damage", "value": 5, "target": "enemy", "tags": ["attack"]}],
      "每次攻击触发一次连环（配合可构造无限连锁用于测试热搜上限）。")
_card("reckless", "莽撞", 1, "attack", "gold",
      [{"type": "damage", "value": 10, "target": "enemy", "tags": ["attack"]},
       {"type": "apply_status", "status": "fragile", "value": 1, "stack": "add", "ticks": 2}],
      "造成 10 点伤害，自身易被击破 2 回合。")
_card("demon_form", "恶魔形态", 3, "power", "gold",
      [{"type": "apply_status", "status": "strength", "value": 3, "stack": "add"},
       {"type": "set_status", "status": "strength_per_turn", "value": 2, "stack": "replace"}],
      "获得 3 点力量；此后每回合开始再获得 2 点力量。")

# 用于“失败解锁”的门控卡（gold 中较高价值的）
_card("sword_dance", "剑舞", 2, "attack", "gold",
      [{"type": "damage", "value": 5, "target": "enemy", "tags": ["attack"]},
       {"type": "damage", "value": 5, "target": "enemy", "tags": ["attack"]},
       {"type": "apply_status", "status": "vulnerable", "value": 2, "stack": "add", "target": "enemy", "ticks": 1}],
      "造成 5 点伤害两次，并施加易伤 1 回合。")

# （加载序保证不存在副作用）


def all_cards():
    return [dict(v) for v in CARDS.values()]


def get_card(cid):
    c = CARDS.get(cid)
    if c is None:
        raise KeyError(f"unknown card: {cid}")
    return dict(c)