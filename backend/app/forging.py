from __future__ import annotations

# 卡牌锻造：在锻造节点花金币为“指定卡牌实例”选择强化分支。
# 同名卡的每张副本是独立实例（uid），各自保存已选分支；引擎在战斗中按实例
# 的 forges 把基础卡牌定义即时换算为“生效卡牌”，因此强化自动贯通战斗/续局/回放。

# 一次锻造的金币花费
FORGE_COST = 25

# 强化分支（数据驱动；实际换算见 effective_card）
# - sharpen 锋锐：攻击伤害 +3；没有攻击效果的牌改为格挡 +3
# - empower 强效：非攻击数值（状态层数/抽牌/回复/能量）+1；没有则攻击伤害 +2
# - refine 精炼：费用 -1（最低 0）
FORGE_BRANCHES = [
    {"id": "sharpen", "name": "锋锐", "tag": "锋",
     "desc": "所有攻击伤害 +3；若该牌没有攻击效果，则格挡 +3。"},
    {"id": "empower", "name": "强效", "tag": "强",
     "desc": "非攻击数值（状态层数/抽牌/回复/能量）+1；若没有此类效果，攻击伤害 +2。"},
    {"id": "refine", "name": "精炼", "tag": "炼",
     "desc": "消耗 -1（最低 0）。"},
]
BRANCH_IDS = {b["id"] for b in FORGE_BRANCHES}

# empower 作用的“非攻击数值”效果类型
_EMPOWER_TYPES = ("apply_status", "set_status", "draw", "heal", "gain_energy")


def public_branches():
    return [dict(b) for b in FORGE_BRANCHES]


def branch_name(bid):
    for b in FORGE_BRANCHES:
        if b["id"] == bid:
            return b["name"]
    return bid


def effective_card(base_card, forges):
    """把基础卡牌定义按实例已选分支换算成生效卡牌（深拷贝，不污染注册表）。

    同一分支可重复选择，效果叠加；重复扣款在 service 层按节点幂等拦截。
    """
    card = dict(base_card)
    effects = [dict(e) for e in card.get("effects", [])]
    for bid in forges or ():
        if bid == "sharpen":
            if any(e.get("type") == "damage" for e in effects):
                for e in effects:
                    if e.get("type") == "damage":
                        e["value"] = e.get("value", 0) + 3
            else:
                for e in effects:
                    if e.get("type") == "gain_block":
                        e["value"] = e.get("value", 0) + 3
        elif bid == "empower":
            targets = [e for e in effects if e.get("type") in _EMPOWER_TYPES]
            if targets:
                for e in targets:
                    e["value"] = e.get("value", 0) + 1
            else:
                for e in effects:
                    if e.get("type") == "damage":
                        e["value"] = e.get("value", 0) + 2
        elif bid == "refine":
            card["cost"] = max(0, card.get("cost", 0) - 1)
    card["effects"] = effects
    return card
