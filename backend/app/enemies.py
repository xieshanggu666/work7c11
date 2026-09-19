from __future__ import annotations

# 敌人 / 首领注册表。skills 为“意图”，每个意图形如：
#   {"name","hint","effects":[...]}, effects 与卡牌同构，由 settlement 引擎解释。
# 首领支持多阶段：phases = [{hp, name, skills:[...]}, ...]

ENEMIES: dict[str, dict] = {}


def _enemy(eid, name, hp, skills, tier, reward_cards=(), reward_gold=30, kind="normal", boss=False, phases=None):
    ENEMIES[eid] = {
        "id": eid, "name": name, "hp": hp, "tier": tier, "kind": kind,
        "reward_cards": list(reward_cards), "reward_gold": reward_gold,
        "skills": [dict(s) for s in skills], "boss": boss,
        "phases": [dict(p) for p in (phases or [])],
    }


def _hit(value, tags=()):
    return {"type": "damage", "value": value, "target": "player", "tags": list(tags)}


def _block(value, target="enemy"):
    return {"type": "gain_block", "value": value, "target": target}


# ---------------- 普通 ---------------- tacit
_enemy("goblin", "哥布林", 18, [
    {"name": "抓挠", "hint": "造成 6 伤害", "effects": [_hit(6)]},
], "basic", reward_cards=["cleave"], reward_gold=25)

_enemy("wolf", "邪狼", 24, [
    {"name": "撕咬", "hint": "造成 7 伤害", "effects": [_hit(7)]},
    {"name": "嚎叫", "hint": "获得力量", "effects": [
        {"type": "apply_status", "status": "strength", "value": 2, "stack": "add", "target": "enemy"}]},
], "basic", reward_cards=["heavy_blow"], reward_gold=30)

_enemy("brute", "黑铁兵", 30, [
    {"name": "挥砍", "hint": "造成 9 伤害", "effects": [_hit(9)]},
    {"name": "格挡", "hint": "获得 8 格挡", "effects": [_block(8)]},
], "advanced", reward_cards=["shield_bash"], reward_gold=35)

_enemy("maggot", "疫蛆", 20, [
    {"name": "喷溅", "hint": "造成 5 伤害，施加易碎", "effects": [_hit(5), {
        "type": "apply_status", "status": "fragile", "value": 1, "stack": "add", "ticks": 2,
        "target": "player"}]},
], "advanced", reward_cards=["iron_wave"], reward_gold=28)

_enemy("vampire", "血裔", 26, [
    {"name": "吸取", "hint": "造成 6 伤害并回血 3", "effects": [_hit(6), {
        "type": "heal", "value": 3, "target": "enemy"}]},
], "advanced", reward_cards=["blood_echo"], reward_gold=32)

_enemy("echo_knight", "回响剑士", 34, [
    {"name": "连环斩", "hint": "每次攻击触发一次", "effects": [
        {"type": "damage", "value": 4, "target": "player", "tags": ["attack"]},
        {"type": "damage", "value": 4, "target": "player", "tags": ["attack"]}]},
], "advanced", reward_cards=["swift"], reward_gold=38)

# ---------------- 精英 ----------------
_enemy("elite_warlord", "战团长", 45, [
    {"name": "重拳", "hint": "造成 12 伤害", "effects": [_hit(12)]},
    {"name": "横扫", "hint": "造成 8 伤害两次", "effects": [_hit(8), _hit(8)]},
], "gold", reward_cards=["battle_trance"], reward_gold=50)

# ---------------- 首领（多阶段） ----------------
_defender_skills = [
    {"name": "尾锤", "hint": "造成 14 伤害", "effects": [_hit(14)]},
    {"name": "石肤", "hint": "获得 16 格挡", "effects": [_block(16)]},
]
_enemy("boss_ancient", "远古守卫", 60, _defender_skills, "gold",
       reward_cards=[], reward_gold=100, kind="boss", boss=True,
       phases=[
           {"hp": 60, "name": "苏醒", "skills": _defender_skills},
           {"hp": 55, "name": "狂暴", "skills": [
               {"name": "狂暴尾锤", "hint": "造成 18 伤害", "effects": [_hit(18)]},
               {"name": "野蛮冲撞", "hint": "造成 12 伤害并易碎", "effects": [_hit(12), {
                   "type": "apply_status", "status": "fragile", "value": 1, "stack": "add", "ticks": 3,
                   "target": "player"}]},
               {"name": "石肤·多重", "hint": "获得 20 格挡", "effects": [_block(20)]},
           ]},
       ])


def all_enemies():
    return [dict(v) for v in ENEMIES.values()]


def get_enemy(eid):
    e = ENEMIES.get(eid)
    if e is None:
        raise KeyError(f"unknown enemy: {eid}")
    return dict(e)