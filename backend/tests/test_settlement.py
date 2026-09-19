"""统一结算队列核心机制：连锁触发 / 状态叠加 / 死亡打断。"""
import pytest

from app.engine import Battle
from app.enemies import get_enemy
from app.cards import get_card


def make_battle(deck=None, p_hp=75):
    run_state = {
        "max_health": 75, "health": p_hp, "deck": deck or ["strike", "strike"],
        "relics": {}, "base_energy": 3,
    }
    b = Battle(run_state, get_enemy("goblin"), seed=1, battle_index=1)
    b.start_turn()
    return b


def _put_in_hand(b, cid):
    if cid not in b.hand:
        b.hand.append(cid)


def test_chain_trigger_echo():
    """回响：攻击触发再攻击（连锁）。"""
    b = make_battle(deck=["strike", "blood_echo"])
    _put_in_hand(b, "blood_echo")
    log = b.play_card(get_card("blood_echo"))
    dmg_events = [e for e in log if e["action"] == "damage" and e["source"] == "player"]
    # 血之回响给予 1 层回响：原始 1 次 + 回响 1 次 = 2 次
    assert len(dmg_events) == 2


def test_status_stacking_add():
    """状态叠加：力量 add 两次再计算伤害翻倍加成。"""
    b = make_battle(deck=["strike", "flex", "flex"])
    _put_in_hand(b, "flex")
    b.play_card(get_card("flex"))
    _put_in_hand(b, "flex")
    b.play_card(get_card("flex"))
    st = b.entities["player"]["statuses"]["strength"]
    assert st["value"] == 4
    # 用打击：6 + 力量4 = 10
    _put_in_hand(b, "strike")
    log = b.play_card(get_card("strike"))
    dmg = [e["value"] for e in log if e["action"] == "damage"]
    assert dmg and dmg[0] == 10


def test_status_stacking_vulnerable_multiplier():
    """易伤：对易伤目标的伤害 ×1.5，且多次施加减值叠加。"""
    b = make_battle(deck=["strike"])
    from app.engine import _apply_status
    _apply_status(b.entities["enemy"], "vulnerable", 1, "add")
    _apply_status(b.entities["enemy"], "vulnerable", 1, "add")
    assert b.entities["enemy"]["statuses"]["vulnerable"]["value"] == 2
    _put_in_hand(b, "strike")
    log = b.play_card(get_card("strike"))
    dmg = [e["value"] for e in log if e["action"] == "damage"]
    assert dmg[0] == 9  # 6 * 1.5


def test_death_interrupt_cancels_pending():
    """死亡打断：目标死后，仍指向它的连锁事件被剔除，不再结算。"""
    b = make_battle(deck=["heavy_blow", "heavy_blow"])
    b.entities["enemy"]["hp"] = 20
    b.entities["enemy"]["block"] = 0
    b.entities["player"]["statuses"]["echo"] = {"mode": "add", "value": 5, "ticks": None}
    _put_in_hand(b, "heavy_blow")
    log = b.play_card(get_card("heavy_blow"))  # 14 伤害 -> 20-14=6
    b.collect_deaths(b.queue)
    _put_in_hand(b, "heavy_blow")
    log2 = b.play_card(get_card("heavy_blow"))  # 命中 -> 6-14 <=0 死
    b.collect_deaths(b.queue)
    assert not b.enemy["alive"]
    # 连锁：死亡后不应再有指向 enemy 的结算追加（回响被打断）
    for e in b.queue.pending:
        assert e.target != "enemy"


def test_end_turn_returns_ordered_enemy_log():
    """敌方回合：end_turn 返回按结算顺序排列的事件日志与意图名。"""
    b = make_battle()
    hp_before = b.entities["player"]["hp"]
    logs, intent = b.end_turn()
    assert intent is not None and intent["name"]
    # 哥布林只有“抓挠”：恰好一条指向玩家的伤害事件
    dmg = [e for e in logs if e["action"] == "damage" and e["target"] == "player"]
    assert len(dmg) == 1 and dmg[0]["source"] == "enemy"
    assert b.entities["player"]["hp"] == hp_before - dmg[0]["value"]


def test_enemy_multi_effect_skill_fully_settles():
    """多效果技能完整结算：血裔“吸取”= 伤害 + 自身回血（此前只结算首个效果）。"""
    run_state = {"max_health": 75, "health": 75, "deck": ["strike", "strike"],
                 "relics": {}, "base_energy": 3}
    b = Battle(run_state, get_enemy("vampire"), seed=1, battle_index=1)
    b.start_turn()
    b.enemy["hp"] = b.enemy["max_hp"] - 5  # 留出回血空间
    hp_p, hp_e = b.entities["player"]["hp"], b.enemy["hp"]
    logs, intent = b.end_turn()
    assert intent["name"] == "吸取"
    actions = [e["action"] for e in logs]
    assert actions == ["damage", "heal"]  # 结算顺序与技能效果声明一致
    assert b.entities["player"]["hp"] == hp_p - 6
    assert b.enemy["hp"] == hp_e + 3