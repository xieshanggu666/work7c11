"""无限连锁封顶：结算队列触发计数达到上限后中断，不卡死。"""
from app.settlement import EffectEvent, SettlementQueue
from app.engine import Battle
from app.enemies import get_enemy


class _FakeEngine:
    """恒返回自我复制的子事件 -> 永不终止的假引擎，用于逼出上限。"""

    def resolve(self, ev):
        return [EffectEvent("boom", target="x", value=1)]

    def is_dead(self, key):
        return False

    def collect_deaths(self, queue):
        pass


def test_queue_never_terminates_but_capped():
    q = SettlementQueue(_FakeEngine(), limit=100)
    q.push(EffectEvent("boom", target="x", value=1))
    log = q.run()
    # 不会死循环；按上限中断
    assert q.counter == 100
    assert q.truncated is True
    # 最后一条是 truncated 标记
    assert log[-1]["action"] == "truncated"


def test_resolve_never_hangs_from_huge_echo():
    """真实引擎 + 极高回响连击在有限血量目标下正常终止（目标死亡打断连锁）。"""
    b = Battle({"max_health": 75, "health": 75, "deck": ["blood_echo"],
                "relics": {}, "base_energy": 3}, get_enemy("echo_knight"), seed=5, battle_index=1)
    b.start_turn()
    b.entities["enemy"]["hp"] = 30
    b.entities["player"]["statuses"]["echo"] = {"mode": "add", "value": 200, "ticks": None}
    if "blood_echo" not in b.hand:
        b.hand.append("blood_echo")
    from app.cards import get_card
    log = b.play_card(get_card("blood_echo"))
    # 目标死亡即停止，不得触发 truncated，也不会卡死
    assert b.enemy["alive"] is False
    assert b.truncated is False