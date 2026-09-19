from __future__ import annotations

import copy
import hashlib
import json
import random
import threading
import uuid

from . import db
from . import enemies as enemies_mod
from . import mapgen
from . import rewards as rewards_mod
from . import forging as forging_mod
from . import shop as shop_mod
from .cards import all_cards, get_card
from .engine import Battle, _statuses_public
from .forging import FORGE_COST, effective_card, branch_name

# 规则版本：引擎/结算/存档结构发生语义变化时递增。
# 建局写入 run 状态、每个动作事件携带 ver；回放据此标记录制版本与旧日志兼容。
RULES_VERSION = "2.0.0"

# 初始牌组：卡牌 id 列表；建局时展开为独立实例（同名卡各持一份成长状态）
START_DECK = ["strike", "strike", "strike", "strike", "guard", "guard", "guard"]
INIT_LOCKED = ["heavy_blow", "cleave", "shield_bash", "pommel", "battle_trance",
               "flex", "iron_wave", "flurry", "adrenaline", "swift", "blood_echo",
               "reckless", "demon_form", "sword_dance"]


class DuplicateReward(Exception):
    pass


class InvalidAction(Exception):
    pass


class StateConflict(Exception):
    """行动提交时存档已被并发请求/进程改写（乐观锁 CAS 失败）。"""


class ShopSoldOut(Exception):
    """商店货架项已售出（重复购买/重复移除同一卡牌实例）。"""
    pass


# 每个 run 一把进程内串行锁：同一局的并发行动排队进同一条原子事务，
# 杜绝“两个请求同时读到旧状态、后写覆盖先写”的丢失更新。
_run_locks_guard = threading.Lock()
_run_locks: dict[str, threading.RLock] = {}


def _run_lock(run_id):
    with _run_locks_guard:
        lk = _run_locks.get(run_id)
        if lk is None:
            lk = threading.RLock()
            _run_locks[run_id] = lk
        return lk


def _make_instances(ids):
    """把卡牌 id 列表展开为 (uid 列表, 实例表, 下一序号)。"""
    uids, instances = [], {}
    for cid in ids:
        uid = f"c{len(uids) + 1}"
        uids.append(uid)
        instances[uid] = {"id": cid, "forges": []}
    return uids, instances


def _new_run_state(seed):
    deck_uids, instances = _make_instances(START_DECK)
    return {
        "seed": seed,
        "rules_version": RULES_VERSION,
        "status": "in_progress",
        "position": "start",
        "max_health": 75,
        "health": 75,
        "base_energy": 3,
        "deck": deck_uids,             # 手牌引用（uid）
        "card_instances": instances,  # uid -> {id, forges:[分支id]}
        "next_card_seq": len(deck_uids) + 1,  # uid 单调发号器
        "gold": 0,
        "relics": {},
        "in_battle": False,
        "battle_index": 0,
        "battle": None,
        "reward_options": [],
        "reward_claimed": True,
        "forge_claimed": True,        # 当前节点是否已完成锻造
        "shop": None,                 # 商店节点库存与交易记录（离开节点即清空）
        "events_log": [],
        "truncated": False,
    }


def _migrate_state(run):
    """旧档兼容：把裸 id 牌组升级为卡牌实例（同名卡获得独立 uid 与成长状态）。

    旧档可能在战斗中：牌堆/手牌/弃牌堆里仍是裸 id，此时按牌组顺序发号 uid，
    再把牌堆中的每次出现映射到该 id 的 uid 队列，洗牌布局与确定性保持不变。
    """
    changed = False
    if "card_instances" not in run:
        instances = {}
        deck_uids = []
        free_uids = {}  # card_id -> 尚未分配到牌堆的 uid 队列
        for cid in run.get("deck", []):
            uid = f"c{len(instances) + 1}"
            instances[uid] = {"id": cid, "forges": []}
            deck_uids.append(uid)
            free_uids.setdefault(cid, []).append(uid)

        b = run.get("battle")
        if b:
            for pile in ("draw_pile", "hand", "discard"):
                mapped = []
                for ref in b.get(pile, []):
                    if not isinstance(ref, str) or ref not in free_uids:
                        mapped.append(ref)
                        continue
                    queue = free_uids[ref]
                    mapped.append(queue.pop(0) if queue else ref)
                b[pile] = mapped
            b["card_instances"] = instances
        run["deck"] = deck_uids
        run["card_instances"] = instances
        run["next_card_seq"] = len(instances) + 1
        changed = True
    # 旧档补记当前规则版本（仅标注；旧动作日志仍按 legacy 处理不做哈希校验）
    if "rules_version" not in run:
        run["rules_version"] = RULES_VERSION
        changed = True
    run.setdefault("forge_claimed", True)
    run.setdefault("shop", None)
    return changed


def get_profile_unlocked():
    prof = db.get_profile()
    if prof is None:
        return {"unlocked": list(START_DECK), "locked": list(INIT_LOCKED)}
    return {"unlocked": list(prof.get("unlocked", START_DECK)),
            "locked": list(prof.get("locked", INIT_LOCKED))}


def _unlocked_view(prof):
    """由调用方持有的 profile 快照构造解锁视口（事务内复用，避免读到未提交数据）。"""
    if prof is None:
        return {"unlocked": list(START_DECK), "locked": list(INIT_LOCKED)}
    return {"unlocked": list(prof.get("unlocked", START_DECK)),
            "locked": list(prof.get("locked", INIT_LOCKED))}


def create_run(seed=None):
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    run_id = uuid.uuid4().hex[:12]
    state = _new_run_state(seed)
    map_data = mapgen.generate_map(seed)
    # 建局的 run 行与 create 事件同一事务：要么都可见，要么都不存在，回放不会少头
    with db.transaction() as t:
        t.insert_run(run_id, state["seed"], state["status"], state["position"], map_data, state)
        t.insert_event(run_id, 1, "create", {
            "seed": state["seed"], "ver": RULES_VERSION, "ckpt": state_checkpoint(state),
        })
        prof = t.get_profile()
    return _public_view(state, map_data, run_id, unlocked=_unlocked_view(prof))


def load_run(run_id):
    return db.load_run(run_id)


def _require_run(run_id):
    run = db.load_run(run_id)
    if run is None:
        raise InvalidAction("run not found")
    return run


# ---------- 敌人解析 ----------
def _enemy_by_node(node_data):
    eid = node_data["enemy"]
    return enemies_mod.get_enemy(eid)


# ---------- 战斗绑定 ----------
def _build_battle(run_state, node_data):
    enemy_def = _enemy_by_node(node_data)
    relic = run_state["relics"]
    boss_hp_bonus = 0
    if node_data["type"] == mapgen.BOSS and "boss_hp_bonus" in relic:
        boss_hp_bonus = relic["boss_hp_bonus"]
    battle = Battle(
        {"max_health": run_state["max_health"], "health": run_state["health"],
         "deck": run_state["deck"], "relics": relic, "base_energy": run_state["base_energy"]},
        enemy_def, seed=run_state["seed"], battle_index=run_state["battle_index"],
        boss_hp_bonus=boss_hp_bonus,
        card_instances=run_state.get("card_instances", {}),
    )
    battle.start_turn()
    return battle


def _load_battle(run_state):
    bstate = run_state["battle"]
    enemy_def = enemies_mod.get_enemy(bstate["enemy"])
    # 战斗内实例表优先（旧档迁移时写入），否则用 run 级实例表
    instances = bstate.get("card_instances", run_state.get("card_instances", {}))
    battle = Battle.from_state(bstate, enemy_def, run_state["seed"],
                               health=run_state["health"], max_health=run_state["max_health"])
    battle.card_instances = dict(instances)
    return battle


# ---------- 行动 ----------
def act(run_id, action):
    """在线行动：校验 -> 纯状态推演（无 DB）-> 原子提交（存档 + 日志 + 解锁 + 幂等）。

    并发安全：
      1. 每局一把进程内 RLock 串行化同一 run 的请求（跨 run 不互相阻塞）；
      2. 提交走单条 BEGIN IMMEDIATE 事务，runs 用 revision 乐观锁 CAS，
         跨进程/多实例部署下被抢先写入时抛 StateConflict（409），绝不覆盖；
      3. req_id（客户端重试令牌）命中时直接回放首次响应，不重复生效/扣款。

    原子性：存档、动作日志、失败解锁奖励、幂等记录要么全部提交，要么全部回滚；
    校验失败（InvalidAction/DuplicateReward/ShopSoldOut）发生在推演阶段，
    根本不会进入写事务，保证失败请求零副作用。

    纯推演部分（_apply_action）与回放共享同一条代码路径，保证“玩的时候”
    和“回放重建”永远使用同一套规则。
    """
    req_id = action.get("req_id")
    with _run_lock(run_id):
        with db.transaction() as t:
            # 幂等：重复/重试请求（双击、网络重发）直接返回首次结果，不再推演一次
            if req_id:
                cached = t.get_idempotent(run_id, req_id)
                if cached is not None:
                    return dict(cached["response"], duplicate=True)

            rec = t.load_run(run_id)
            if rec is None:
                raise InvalidAction("run not found")
            run = rec["state"]
            map_data = rec["map"]
            revision = rec["revision"]
            # 旧档兼容：迁移与本次行动结果在同一事务落库，迁移不会单独半途丢失
            _migrate_state(run)
            if run["status"] != "in_progress":
                raise InvalidAction(f"run already ended ({run['status']})")

            a = action.get("action")
            ctx = {"lost": False}
            log = _apply_action(run, a, action, map_data, grant_unlocks=True, ctx=ctx)

            # 战败解锁奖励：在同一事务里读改写 profile，与存档/日志一起提交
            prof = _commit_loss_unlock(t, run) if ctx["lost"] else t.get_profile()

            ckpt = state_checkpoint(run)
            seq = t.next_seq(run_id)
            payload = {
                "node": action.get("node"), "card": action.get("card"),
                "option": action.get("option"), "branch": action.get("branch"),
                "kind": action.get("kind"), "sku": action.get("sku"),
                "ver": RULES_VERSION, "ckpt": ckpt,
            }
            try:
                t.save_run_cas(run_id, run["status"], run["position"], run, revision)
                t.insert_event(run_id, seq, a, payload)
            except db.Conflict as e:
                # 跨进程并发：revision 已被推进或日志序号被占，整体回滚，交客户端重试
                raise StateConflict(str(e)) from e

            view = _public_view(run, map_data, run_id, unlocked=_unlocked_view(prof))
            response = {"seq": seq, "log": log, "run": view, "checkpoint": ckpt}
            if req_id:
                t.put_idempotent(run_id, req_id, seq, response)
            return response


def _apply_action(run, a, action, map_data, grant_unlocks=False, ctx=None):
    """对内存中的 run 状态执行一个动作（纯函数语义）。

    map_data 由调用方持有（在线=存档地图；回放=按种子重新生成的同一地图）。
    grant_unlocks=False（回放/模拟）时，战败也绝不触发任何解锁副作用；
    grant_unlocks=True（在线）时只在 ctx 里标记 lost，由调用方在同一写事务内
    完成 profile 读改写（保证解锁与存档/日志原子提交）。
    不读写数据库、不迁移存档——调用方负责准备好已迁移的状态。
    """
    if a == "choose_node":
        _choose_node(run, map_data, action["node"])
        return []
    if a == "play":
        return _play(run, action["card"], grant_unlocks=grant_unlocks, ctx=ctx)
    if a == "end_turn":
        return _end_turn(run, grant_unlocks=grant_unlocks, ctx=ctx)
    if a == "claim_reward":
        return _claim_reward(run, action["option"])
    if a == "forge":
        return _forge(run, action.get("card"), action.get("branch"))
    if a == "shop_buy":
        return _shop_buy(run, action.get("kind"), action.get("sku"))
    if a == "shop_remove":
        return _shop_remove(run, action.get("card"))
    raise InvalidAction(f"unknown action {a}")


def _choose_node(run, map_data, node):
    from_current = map_data["routes"].get(run["position"], [])
    if run["position"] != mapgen.BOSS and node not in from_current:
        raise InvalidAction(f"node {node} unreachable from {run['position']}")
    node_data = map_data["nodes"][node]
    run["position"] = node
    # 每进入一个新节点都清掉上个节点的商店库存（商店仅在其节点内有效）
    run["shop"] = None

    t = node_data["type"]
    if t in (mapgen.ENCOUNTER, mapgen.ELITE, mapgen.BOSS):
        run["in_battle"] = True
        run["battle_index"] += 1
        battle = _build_battle(run, node_data)
        run["battle"] = battle.dump()
        run["health"] = battle.entities["player"]["hp"]
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        battle_start = battle.to_snapshot()["hand"]
        run["events_log"].append({"at": f"battle:{node}:{run['battle_index']}", "battle": True})
    elif t == mapgen.REST:
        heal = max(1, int(run["max_health"] * 0.2))
        run["health"] = min(run["max_health"], run["health"] + heal)
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        run["events_log"].append({"at": f"rest:{node}", "heal": heal})
    elif t == mapgen.REWARD:
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = rewards_mod.relic_choice_options(run["seed"] + run["battle_index"] * 7)
        run["reward_claimed"] = False
        run["forge_claimed"] = True
    elif t == mapgen.FORGE:
        # 锻造节点：进入即待锻造，玩家可花金币为一张牌选择强化分支（仅一次）
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = False
        run["events_log"].append({"at": f"forge:{node}"})
    elif t == mapgen.SHOP:
        # 旅途商店：按 (种子, 节点) 确定性生成库存；交易记录随库存保存，续局/回放一致
        run["in_battle"] = False
        run["battle"] = None
        run["reward_options"] = []
        run["reward_claimed"] = True
        run["forge_claimed"] = True
        stock_seed = (run["seed"] * 10007 + node_data["row"] * 131 + ord(node[0])) & 0xFFFFFFFF
        owned_cards = {inst["id"] for inst in run.get("card_instances", {}).values()}
        run["shop"] = shop_mod.generate_stock(stock_seed, owned_cards, set(run["relics"]))
        run["events_log"].append({"at": f"shop:{node}",
                                  "cards": len(run["shop"]["cards"]),
                                  "relics": len(run["shop"]["relics"])})
    elif t == "start":
        run["reward_claimed"] = True
        run["forge_claimed"] = True


def _battle_or_raise(run):
    if not run["in_battle"] or not run["battle"]:
        raise InvalidAction("not in battle")


def _play(run, card_ref, grant_unlocks=True, ctx=None):
    _battle_or_raise(run)
    battle = _load_battle(run)
    if not battle.in_turn:
        raise InvalidAction("not player turn")
    # card 为手牌引用（uid；旧版 v1 动作日志记录的是裸卡牌 id）
    card_ref = _resolve_hand_ref(run, battle, card_ref)
    if card_ref not in battle.hand:
        raise InvalidAction("hand does not contain that card")
    card = battle._card_def(card_ref)
    if card["cost"] > battle.energy:
        raise InvalidAction("not enough energy")
    battle.energy -= card["cost"]
    try:
        log = battle.play_card(card_ref)
    except ValueError as e:
        raise InvalidAction(str(e))
    return _after_battle_step(run, battle, log, grant_unlocks, ctx)


def _resolve_hand_ref(run, battle, ref):
    """动作里的卡牌引用 -> 手牌引用。

    新档/现版日志：uid，原样返回。
    旧版日志（v1，卡牌实例化之前）：记录的是裸卡牌 id；按 run 级实例表把它
    解析为手牌中同 id 的某个 uid（同 id 实例在未锻造时完全等价），实现旧日志回放。
    """
    if ref in battle.hand:
        return ref
    instances = run.get("card_instances", {})
    if ref in instances:  # 卡在实例表但不在手牌（非法动作，保持原校验语义）
        return ref
    for uid in battle.hand:
        inst = instances.get(uid)
        if inst is not None and inst.get("id") == ref:
            return uid
    return ref  # 解析不到：交回上层的“手牌不存在”校验


def _end_turn(run, grant_unlocks=True, ctx=None):
    _battle_or_raise(run)
    battle = _load_battle(run)
    run["reward_claimed"] = True
    enemy_log, intent = battle.end_turn()
    log = []
    if intent is not None:
        # 敌方回合标记（含技能名），前端据此播放“敌方行动”横幅
        log.append({"action": "enemy_turn", "target": "player", "value": 0,
                    "source": "enemy", "tags": ["system"],
                    "extra": {"name": intent.get("name", "")}})
    # 敌方结算事件按结算顺序入日志，前端依序播放连锁动画
    log.extend(enemy_log)
    return _after_battle_step(run, battle, log, grant_unlocks, ctx)


def _after_battle_step(run, battle, log, grant_unlocks=True, ctx=None):
    snap = battle.to_snapshot()
    result = battle.battle_result()
    run["health"] = battle.entities["player"]["hp"]
    if result == "ongoing":
        run["battle"] = battle.dump()
        log.append({"snapshot": snap})
        return log

    # 战斗结束
    run["in_battle"] = False
    if result == "won":
        run["battle"] = None
        # 用当前节点敌人掉落生成奖励
        enemy = battle.enemy_def
        run["reward_options"] = rewards_mod.battle_reward_options(
            run["seed"] + run["battle_index"], enemy, run)
        run["reward_claimed"] = False
        log.append({"result": "won", "snapshot": snap})
        if enemy.get("boss"):
            run["status"] = "won"
            run["reward_options"] = []
            run["reward_claimed"] = True
            log.append({"result": "run_won"})
    else:
        run["battle"] = None
        run["status"] = "lost"
        log.append({"result": "lost", "snapshot": snap})
        # 在线路径仅做“标记”，真正的 profile 读改写出 act 在同一事务内完成，
        # 保证解锁与存档/日志原子提交；回放/模拟（grant_unlocks=False）完全无副作用
        if grant_unlocks:
            if ctx is not None:
                ctx["lost"] = True
            else:  # 兼容直接以在线语义调用纯推演的旧用法
                _grant_unlock_on_loss(run)
    return log


def _claim_reward(run, option_idx):
    if run["reward_claimed"]:
        raise DuplicateReward("reward already claimed")
    if run["status"] != "in_progress":
        raise InvalidAction("not in progress")
    opts = run["reward_options"]
    if option_idx < 0 or option_idx >= len(opts):
        raise InvalidAction("bad reward option")
    opt = opts[option_idx]
    _apply_option(run, opt)
    run["reward_claimed"] = True
    run["reward_options"] = []
    return [{"reward_claimed": opt["name"]}]


def _apply_option(run, opt):
    for eff in opt.get("effects", []):
        _apply_option_effect(run, eff)


def _apply_option_effect(run, eff):
    t = eff["type"]
    if t == "add_card":
        _add_card_instance(run, eff["card"])
    elif t == "gold":
        run["gold"] += eff["value"]
    elif t == "heal_run":
        run["health"] = min(run["max_health"], run["health"] + eff["value"])
    elif t == "relic_set":
        run["relics"][eff["relic"]] = eff.get("value", 1)


def _add_card_instance(run, cid):
    """奖励入牌：发放带独立成长状态的新卡牌实例（同名卡互不共享锻造）。"""
    seq = run.get("next_card_seq", len(run.get("card_instances", {})) + 1)
    uid = f"c{seq}"
    run["next_card_seq"] = seq + 1
    run.setdefault("card_instances", {})[uid] = {"id": cid, "forges": []}
    run["deck"].append(uid)
    return uid


# ---------- 卡牌锻造 ----------
def _forge(run, card_uid, branch):
    # 仅在尚未锻造的锻造节点可操作：forge_claimed 承担幂等键，重复请求不重复扣款（409）
    if run.get("forge_claimed", True):
        raise DuplicateReward("forge already used at this node")
    if not card_uid or card_uid not in run.get("card_instances", {}):
        raise InvalidAction("unknown card instance")
    if branch not in forging_mod.BRANCH_IDS:
        raise InvalidAction("unknown forge branch")
    if run["gold"] < FORGE_COST:
        raise InvalidAction("not enough gold")
    # 先校验全部通过再扣款，避免失败请求产生任何副作用
    run["gold"] -= FORGE_COST
    inst = run["card_instances"][card_uid]
    inst.setdefault("forges", []).append(branch)
    run["forge_claimed"] = True
    run["events_log"].append({
        "at": f"forge:{run['position']}", "card": inst["id"], "uid": card_uid, "branch": branch,
    })
    base = get_card(inst["id"])
    eff = effective_card(base, inst["forges"])
    return [{"forged": {"uid": card_uid, "card": inst["id"], "branch": branch,
                        "cost": eff["cost"], "gold_left": run["gold"]}}]


# ---------- 旅途商店 ----------
def _active_shop(run):
    if run.get("in_battle") or not run.get("shop"):
        raise InvalidAction("not at a shop")
    return run["shop"]


def _find_offer(shop, kind, sku):
    bucket = shop["cards"] if kind == "card" else shop["relics"] if kind == "relic" else None
    if bucket is None:
        raise InvalidAction("unknown shop shelf (kind must be card/relic)")
    item = next((it for it in bucket if it["sku"] == sku), None)
    if item is None:
        raise InvalidAction("unknown shop item")
    if item["sold"]:
        # 售罄（含重复购买）：不扣款
        raise ShopSoldOut("item already sold")
    return item


def _commit_shop_tx(run, mutate, record):
    """统一交易：先对 run 做深拷贝快照，执行扣款与变更；任意异常都整体回退到快照，
    保证失败请求零副作用（不扣款、不改牌组/遗物/库存）。成功才写入交易记录。"""
    snapshot = copy.deepcopy(run)
    try:
        result = mutate(run)
    except Exception:
        run.clear()
        run.update(snapshot)
        raise
    record_obj = record(result)
    run["shop"]["tx"].append(record_obj)
    return [{"shop_tx": record_obj}]


def _shop_buy(run, kind, sku):
    shop = _active_shop(run)
    item = _find_offer(shop, kind, sku)  # 非法货架/售罄在扣款前拦截
    price = item["price"]
    if run["gold"] < price:
        raise InvalidAction("not enough gold")  # 校验先于扣款，无副作用

    if kind == "card":
        cid = item["card"]
        # 货架生成时按未持有过滤；若已通过其它途径获得同名卡，仍允许购买（独立新实例）
        def mutate(r):
            r["gold"] -= price
            uid = _add_card_instance(r, cid)
            next(it for it in r["shop"]["cards"] if it["sku"] == sku)["sold"] = True
            return {"uid": uid}
    else:
        rid = item["relic"]
        if rid in run["relics"]:
            raise ShopSoldOut("relic already owned")
        relic = shop_mod.relic_def(rid)

        def mutate(r):
            r["gold"] -= price
            for eff in relic.get("effects", []):
                _apply_option_effect(r, eff)
            next(it for it in r["shop"]["relics"] if it["sku"] == sku)["sold"] = True
            return {"relic": rid}

    return _commit_shop_tx(
        run, mutate,
        lambda res: {"type": "buy", "kind": kind, "sku": sku, "price": price,
                     "gold_left": run["gold"], **res})


def _shop_remove(run, card_uid):
    shop = _active_shop(run)
    if not card_uid or card_uid not in run.get("card_instances", {}):
        raise InvalidAction("unknown card instance")
    if len(run["deck"]) <= shop_mod.REMOVE_MIN_DECK:
        raise InvalidAction("deck too small to remove")
    cost = shop["remove"]["cost"]
    if run["gold"] < cost:
        raise InvalidAction("not enough gold")  # 校验先于扣款，无副作用
    cid = run["card_instances"][card_uid]["id"]

    def mutate(r):
        r["gold"] -= cost
        r["deck"].remove(card_uid)
        del r["card_instances"][card_uid]
        r["shop"]["remove"]["used"] += 1
        r["shop"]["remove"]["cost"] = shop_mod.next_remove_cost(r["shop"]["remove"]["used"])
        return {"uid": card_uid, "card": cid, "deck_size": len(r["deck"])}

    return _commit_shop_tx(
        run, mutate,
        lambda res: {"type": "remove", **res, "price": cost, "gold_left": run["gold"]})


def _commit_loss_unlock(t, run):
    """在写事务 t 内完成战败解锁：读 profile -> 选卡 -> 写回，全部同一事务。

    返回写入后的 profile 快照（供视口渲染）。
    """
    prof = t.get_profile()
    unlocked = list(prof["unlocked"]) if prof else list(START_DECK)
    locked = list(prof["locked"]) if prof else list(INIT_LOCKED)
    pool = [c for c in locked if all_cards_locked().get(c)]
    if pool:
        rng = random.Random(run["seed"] + run["battle_index"])
        cid = pool[rng.randrange(len(pool))]
        unlocked.append(cid)
        locked.remove(cid)
        prof = {"unlocked": unlocked, "locked": locked}
        t.upsert_profile(prof)
    return prof


def _grant_unlock_on_loss(run):
    """兼容旧用法：独立事务发解锁（仅兜底，正常在线路径走 _commit_loss_unlock 原子提交）。"""
    with db.transaction() as t:
        return t.get_profile(), _commit_loss_unlock(t, run)


def all_cards_locked():
    return {c["id"]: c for c in all_cards()}


# ---------- 视口 ----------
def resume(run_id):
    """续局读档。旧档迁移与落库放在同一事务里（迁移写 + 版本号更新原子完成）。"""
    with _run_lock(run_id):
        with db.transaction() as t:
            rec = t.load_run(run_id)
            if rec is None:
                raise InvalidAction("run not found")
            changed = _migrate_state(rec["state"])
            if changed:
                t.save_run(rec["id"], rec["state"]["status"],
                           rec["state"]["position"], rec["state"])
            prof = t.get_profile()
        return _public_view(rec["state"], rec["map"], rec["id"],
                            unlocked=_unlocked_view(prof))


# ---------- 规则版本与校验点 ----------
# 不参与状态校验的瞬时/派生字段：events_log 只用于事件叙述，不影响规则推演
_CKPT_SKIP_KEYS = {"events_log"}


def state_checkpoint(run):
    """权威状态校验点：对完整 run 状态取稳定哈希（SHA-256 截断 16 位）。

    回放每重演一步就与动作日志里记录的 ckpt 比对；不一致说明规则版本变化、
    日志损坏或确定性被破坏（而不是悄悄播一份错误的历史）。
    """
    material = {k: v for k, v in run.items() if k not in _CKPT_SKIP_KEYS}
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def replay(run_id):
    """整局可交互回放。

    从“建局初始状态”开始，按动作日志逐步调用与在线完全相同的纯推演函数
    （_apply_action，grant_unlocks=False），为每个动作产出一帧：
      - view：该动作完成后的完整只读视口（地图/战斗/锻造/商店，结构与 /resume 一致）
      - events：该动作产生的结算事件（战斗动画逐条播放；锻造/交易结果同构）
      - kind/title/summary：时间轴分组与人类可读描述
      - result：战斗/整局在本步结束（won/lost/run_won）
    校验：每步与日志记录的 ckpt 哈希比对；旧日志（无 ckpt/无 ver）标记 legacy
    并跳过校验。整个回放只读内存与已持久化的日志，不写 runs/battle_events/profile，
    战败不触发解锁奖励。
    """
    rec = load_run(run_id)
    if rec is None:
        raise InvalidAction("run not found")
    seed = rec["state"]["seed"]
    map_data = rec["map"]
    events = db.load_events(run_id)

    # 回放起点：重新构造建局时的初始状态（不读、不写、不迁移真实存档）
    sim = _new_run_state(seed)
    initial_ckpt = state_checkpoint(sim)

    steps = []
    checks = []          # 每步校验结果
    legacy_steps = 0
    skipped_errors = 0
    versions = set()

    for ev in events:
        payload = ev.get("payload") or {}
        corrupt = bool(payload.get("_corrupt"))
        ver = payload.get("ver")
        if ver:
            versions.add(ver)
        is_legacy = not ver and not corrupt
        if is_legacy:
            legacy_steps += 1
        recorded = payload.get("ckpt")
        a = ev["action"]

        log, error = [], None
        if corrupt:
            # 异常日志（payload 无法解析）：跳过推演，保留时间轴位置并标注 error
            error = "CorruptEventError: payload is not valid JSON"
            skipped_errors += 1
        elif a == "create":
            # 建局事件只携带种子；初始状态已在循环外构造，不产生状态变化
            pass
        else:
            try:
                log = _apply_action(sim, a, payload, map_data, grant_unlocks=False)
            except Exception as e:  # 损坏/越权动作不抹掉整段回放：断在此步并标注
                error = f"{type(e).__name__}: {e}"
                skipped_errors += 1

        actual = state_checkpoint(sim)
        if error:
            status = "error"
        elif not recorded:
            status = "legacy"          # 旧版日志无校验点：可播放但不保证逐位一致
        elif recorded == actual:
            status = "ok"
        else:
            status = "mismatch"
        checks.append({"seq": ev["seq"], "action": a, "status": status,
                       "recorded": recorded, "actual": actual})

        # 事件与在线 /act 返回的 log 同构（含 snapshot 校正点），前端播放器可复用
        # 同一套结算事件驱动；帧 view 本身已携带权威状态，跳转时直接落帧无需放动画。
        anim_events = [dict(x) for x in log]
        steps.append({
            "seq": ev["seq"],
            "action": a,
            "payload": payload,
            "kind": _step_kind(sim, a, payload, log),
            "title": _step_title(sim, map_data, a, payload, log),
            "summary": _step_summary(a, payload, log),
            "events": anim_events,
            "result": _step_result(log),
            "view": _public_view(sim, map_data, run_id, include_unlocks=False),
            "check": status,
            "legacy": is_legacy,
            "error": error,
        })

    recorded_versions = sorted(versions)
    current = RULES_VERSION
    final_match = all(c["status"] in ("ok", "legacy") for c in checks)
    return {
        # 兼容旧客户端：仍返回扁平动作序列与种子
        "run_id": run_id,
        "seed": seed,
        "actions": events,
        # 交互式回放
        "rules_version": current,
        "recorded_versions": recorded_versions,
        "legacy": legacy_steps > 0 or not recorded_versions,
        "initial": {"checkpoint": initial_ckpt},
        "steps": steps,
        "final_view": _public_view(sim, map_data, run_id, include_unlocks=False),
        "verification": {
            "ok": sum(c["status"] == "ok" for c in checks),
            "legacy": sum(c["status"] == "legacy" for c in checks),
            "mismatch": sum(c["status"] == "mismatch" for c in checks),
            "error": sum(c["status"] == "error" for c in checks),
            "final_match": final_match,
            "checks": checks,
            "skipped_errors": skipped_errors,
        },
        "isolated": True,  # 声明：本次回放无任何存档写入与解锁副作用
    }


def _step_result(log):
    for x in reversed(log):
        if isinstance(x, dict) and x.get("result"):
            return x["result"]
    return None


def _step_kind(sim, action, payload, log):
    if action == "choose_node":
        return "battle_entry" if sim.get("in_battle") else "route"
    if action in ("play", "end_turn"):
        return "battle"
    if action == "claim_reward":
        return "reward"
    if action == "forge":
        return "forge"
    if action in ("shop_buy", "shop_remove"):
        return "trade"
    if action == "create":
        return "create"
    return "other"


def _node_label(map_data, node):
    if not node:
        return ""
    nd = map_data["nodes"].get(node)
    labels = {"encounter": "遭遇", "elite": "精英", "rest": "休息", "reward": "奖励",
              "forge": "锻造", "shop": "商店", "boss": "首领", "start": "营地"}
    return labels.get((nd or {}).get("type"), node)


def _card_name(cid):
    from .cards import CARDS
    c = CARDS.get(cid)
    return c["name"] if c else cid


def _step_title(sim, map_data, action, payload, log):
    if action == "create":
        return "建局"
    if action == "choose_node":
        node = payload.get("node")
        return f"前往{_node_label(map_data, node)}节点"
    if action == "play":
        inst = sim.get("card_instances", {}).get(payload.get("card"))
        cid = inst.get("id") if inst else payload.get("card")
        return f"打出「{_card_name(cid)}」"
    if action == "end_turn":
        for x in log:
            if isinstance(x, dict) and x.get("action") == "enemy_turn":
                who = x.get("extra", {}).get("name")
                return f"结束回合 · 敌方行动：{who}" if who else "结束回合 · 敌方行动"
        return "结束回合"
    if action == "claim_reward":
        for x in log:
            if isinstance(x, dict) and x.get("reward_claimed"):
                return f"领取奖励「{x['reward_claimed']}」"
        return "领取奖励"
    if action == "forge":
        inst = sim.get("card_instances", {}).get(payload.get("card"))
        cname = _card_name(inst["id"]) if inst else (payload.get("card") or "")
        return f"锻造 {cname} · {branch_name(payload.get('branch'))}"
    if action == "shop_buy":
        return f"商店购买（{payload.get('sku')}）"
    if action == "shop_remove":
        return "商店移除卡牌"
    return action


def _step_summary(action, payload, log):
    """时间轴上的简短状态变化描述（金币/牌组/战斗结果/交易）。"""
    if action == "shop_buy" or action == "shop_remove":
        tx = next((x.get("shop_tx") for x in log if isinstance(x, dict) and x.get("shop_tx")), None)
        if tx:
            return f"花费 {tx.get('price')}，余额 {tx.get('gold_left')}"
    if action == "forge":
        f = next((x.get("forged") for x in log if isinstance(x, dict) and x.get("forged")), None)
        if f:
            return f"花费 {FORGE_COST}，余额 {f.get('gold_left')}"
    if action in ("play", "end_turn"):
        r = _step_result(log)
        if r == "won":
            return "战斗胜利"
        if r == "lost":
            return "战斗失败"
        if r == "run_won":
            return "通关！"
        dmg = sum(x.get("value", 0) for x in log
                  if isinstance(x, dict) and x.get("action") in ("damage", "echo_damage"))
        if dmg:
            return f"结算 {len([x for x in log if isinstance(x, dict) and x.get('action')])} 个事件"
    if action == "claim_reward":
        for x in log:
            if isinstance(x, dict) and x.get("reward_claimed"):
                return f"获得「{x['reward_claimed']}」"
    if action == "choose_node" and payload.get("node"):
        return f"位置 → {payload['node']}"
    return ""


def _hand_public(run, bstate):
    """战斗手牌视口：新档给出含生效费用/锻造标记的实例项，旧档回退为裸 id。"""
    instances = bstate.get("card_instances", run.get("card_instances", {}))
    if not instances:
        return list(bstate["hand"])
    out = []
    for ref in bstate["hand"]:
        inst = instances.get(ref)
        if inst is None:
            out.append(ref)
            continue
        eff = effective_card(get_card(inst["id"]), inst.get("forges", []))
        out.append({
            "uid": ref, "id": inst["id"], "cost": eff["cost"],
            "forges": list(inst.get("forges", [])),
        })
    return out


def _public_view(run, map_data, run_id, include_unlocks=True, unlocked=None):
    """只读视口。

    include_unlocks=False（回放）时不读取 profile 库，解锁信息为 None。
    unlocked 非 None 时直接用调用方在事务内持有的 profile 快照（避免事务内
    读到未提交数据，也避免提交后多开一次连接）。
    """
    unlock_view = None
    if include_unlocks:
        unlock_view = unlocked if unlocked is not None else get_profile_unlocked()
    reachable = map_data["routes"].get(run["position"], [])
    snap = None
    if run["in_battle"] and run["battle"]:
        def pub_ent(k):
            e = run["battle"]["entities"][k]
            return {
                "name": e["name"], "hp": e["hp"], "max_hp": e["max_hp"],
                "block": e["block"], "alive": e["alive"],
                "statuses": _statuses_public(e["statuses"]),
            }
        snap = {
            "player": pub_ent("player"),
            "enemy": pub_ent("enemy"),
            "energy": run["battle"]["energy"],
            "max_energy": run["battle"]["max_energy"],
            "turn": run["battle"]["turn"],
            "in_turn": run["battle"]["in_turn"],
            "truncated": run["battle"]["truncated"],
            "hand": _hand_public(run, run["battle"]),
            "enemy_id": run["battle"]["enemy"],
        }
    # 牌组视口：同名卡按实例独立呈现（携带各自锻造分支）
    instances = run.get("card_instances", {})
    deck_view = [{
        "uid": uid, "id": instances[uid]["id"],
        "forges": list(instances[uid].get("forges", [])),
    } for uid in run["deck"] if uid in instances] or list(run["deck"])
    node_data = map_data["nodes"].get(run["position"], {})
    return {
        "run_id": run_id,
        "seed": run["seed"],
        "status": run["status"],
        "position": run["position"],
        "health": run["health"],
        "max_health": run["max_health"],
        "gold": run["gold"],
        "energy": run["battle"]["energy"] if run["in_battle"] else run["base_energy"],
        "deck": deck_view,
        "relics": dict(run["relics"]),
        "reward_options": list(run["reward_options"]),
        "reward_claimed": run["reward_claimed"],
        "forge_available": node_data.get("type") == mapgen.FORGE and not run.get("forge_claimed", True),
        "forge_claimed": bool(run.get("forge_claimed", True)),
        "forge_cost": FORGE_COST,
        "forge_branches": forging_mod.public_branches(),
        "shop_available": node_data.get("type") == mapgen.SHOP and bool(run.get("shop")),
        "shop": shop_mod.public_view(run.get("shop")),
        "in_battle": run["in_battle"],
        "battle": snap,
        "reachable": [map_data["nodes"][n] for n in reachable],
        "map": _map_public(map_data, run["position"]),
        "unlocked_cards": unlock_view,
        "truncated": bool(run["battle"]["truncated"]) if run["in_battle"] and run["battle"] else bool(run.get("truncated", False)),
    }


def _map_public(map_data, position):
    return {
        "nodes": map_data["nodes"],
        "routes": map_data["routes"],
        "start": map_data["start"],
        "boss": map_data["boss"],
        "position": position,
    }