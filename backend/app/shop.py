from __future__ import annotations

import random

from .cards import CARDS
from .rewards import ALL_RELIC_OPTIONS

# 旅途商店：地图上的商人节点。
# 库存由“种子 + 节点位置”确定性生成（续局/回放天然一致）；购买卡牌/遗物、付费移除
# 指定卡牌实例都走统一事务：先做全部校验，再扣款并变更 run 状态，任何异常整体回退。
# 交易结果直接写入 run（金币/牌组/实例表/遗物），因此自动贯通后续战斗、续局与回放。

# 付费移除卡牌实例：基础价格；同一商店每移除一次，下一次价格递增
REMOVE_BASE_COST = 35
REMOVE_COST_GROWTH = 15
# 牌组至少保留的卡牌数（低于该数量不允许再移除）
REMOVE_MIN_DECK = 1

# 卡牌按稀有度分档的售价区间（生成库存时在区间内确定性取价）
CARD_PRICE_RANGE = {
    "advanced": (45, 60),
    "gold": (70, 90),
}

# 商店遗物：复用奖励遗物定义（effects 同构，由 service 统一结算）
SHOP_RELIC_IDS = ["heal_15", "anvil", "curse_core"]
RELIC_PRICE = {
    "heal_15": 40,
    "anvil": 80,
    "curse_core": 90,
}

CARD_OFFER_COUNT = 3
RELIC_OFFER_COUNT = 2


def relic_def(rid):
    return next(r for r in ALL_RELIC_OPTIONS if r["id"] == rid)


def next_remove_cost(used):
    """第 used+1 次移除的价格。"""
    return REMOVE_BASE_COST + used * REMOVE_COST_GROWTH


def generate_stock(stock_seed, owned_card_ids, owned_relic_ids):
    """确定性生成商人库存：若干张尚未持有的进阶/金色卡 + 若干个尚未持有的遗物。

    库存只依赖 (stock_seed, 进入时的持有集合)；持有集合本身由动作序列确定性派生，
    因此同一条动作日志重放必得同一库存。每个货架项带独立 sku 与 sold 标记。
    """
    rng = random.Random(stock_seed)

    card_pool = [cid for cid, c in CARDS.items()
                 if c.get("tier") in CARD_PRICE_RANGE and cid not in owned_card_ids]
    rng.shuffle(card_pool)
    cards = []
    for cid in card_pool[:CARD_OFFER_COUNT]:
        lo, hi = CARD_PRICE_RANGE[CARDS[cid]["tier"]]
        cards.append({
            "sku": f"card:{cid}", "kind": "card", "card": cid,
            "price": rng.randint(lo, hi), "sold": False,
        })

    relic_pool = [rid for rid in SHOP_RELIC_IDS if rid not in owned_relic_ids]
    rng.shuffle(relic_pool)
    relics = [{
        "sku": f"relic:{rid}", "kind": "relic", "relic": rid,
        "price": RELIC_PRICE[rid], "sold": False,
    } for rid in relic_pool[:RELIC_OFFER_COUNT]]

    return {
        "seed": stock_seed,
        "cards": cards,
        "relics": relics,
        "remove": {"cost": REMOVE_BASE_COST, "used": 0},
        "tx": [],          # 本商店的交易记录（扣款/购得/移除，按发生序）
    }


def card_offer_view(item):
    """货架卡牌项 -> 视口（附带渲染所需的卡牌元数据）。"""
    c = CARDS[item["card"]]
    return {
        **item,
        "name": c["name"], "desc": c["desc"], "tier": c["tier"],
        "type": c["type"], "card_cost": c["cost"],
    }


def relic_offer_view(item):
    r = relic_def(item["relic"])
    return {**item, "name": r["name"], "desc": r["desc"]}


def public_view(shop):
    if not shop:
        return None
    return {
        "cards": [card_offer_view(it) for it in shop["cards"]],
        "relics": [relic_offer_view(it) for it in shop["relics"]],
        "remove": {
            "cost": shop["remove"]["cost"],
            "next_cost": next_remove_cost(shop["remove"]["used"]),
            "used": shop["remove"]["used"],
            "min_deck": REMOVE_MIN_DECK,
        },
        "tx": [dict(t) for t in shop["tx"]],
    }
