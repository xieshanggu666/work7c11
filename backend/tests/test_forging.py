"""卡牌锻造：节点强化分支、同名卡独立成长、贯通战斗/奖励/续局/回放、防重复扣款、旧档兼容。"""
import pytest

from app import service, mapgen, db
from app.cards import get_card
from app.forging import FORGE_COST, effective_card


# ---------- 纯函数：分支换算 ----------
def test_effective_card_branches_stack():
    strike = get_card("strike")  # 6 伤 / 1 费
    eff = effective_card(strike, ["sharpen", "refine"])
    assert eff["effects"][0]["value"] == 9   # 6 + 3
    assert eff["cost"] == 0                  # 1 - 1，最低 0
    # 不污染注册表
    assert get_card("strike")["effects"][0]["value"] == 6
    assert get_card("strike")["cost"] == 1


def test_effective_card_empower_and_refine_floor():
    flex = get_card("flex")  # 获得 2 力量
    eff = effective_card(flex, ["empower"])
    assert eff["effects"][0]["value"] == 3
    guard = get_card("guard")  # 5 格挡：sharpen 无攻击效果时转成格挡强化
    assert effective_card(guard, ["sharpen"])["effects"][0]["value"] == 8
    # 精炼不会把费用压成负数
    zero = effective_card(get_card("adrenaline"), ["refine", "refine"])
    assert zero["cost"] == 0


# ---------- 地图/节点 ----------
def _find_forge_path(seed_start=0):
    """返回 (seed, 到锻造节点的完整路径)，最多探两行。"""
    for seed in range(seed_start, seed_start + 500):
        m = mapgen.generate_map(seed)
        for n0 in m["routes"][m["start"]]:
            if m["nodes"][n0]["type"] == mapgen.FORGE:
                return seed, [n0]
            for n1 in m["routes"][n0]:
                if m["nodes"][n1]["type"] == mapgen.FORGE:
                    return seed, [n0, n1]
    raise AssertionError("no forge node")


def _find_enemy_path(seed_start=0):
    for seed in range(seed_start, seed_start + 200):
        m = mapgen.generate_map(seed)
        for n0 in m["routes"][m["start"]]:
            if m["nodes"][n0]["type"] in (mapgen.ENCOUNTER, mapgen.ELITE):
                return seed, [n0]
    raise AssertionError("no enemy node")


def _walk(client, rid, nodes):
    run = None
    for n in nodes:
        run = client.post(f"/api/runs/{rid}/act",
                          json={"action": "choose_node", "node": n}).json()["run"]
    return run


# ---------- 锻造节点：金币校验 + 防重复扣款 ----------
def test_forge_requires_gold_and_deducts_once(client):
    seed, path = _find_forge_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    assert run["forge_available"] is True
    assert run["forge_cost"] == FORGE_COST
    first_uid = run["deck"][0]["uid"]

    # 金币不足 -> 400，不产生任何效果
    poor = client.post(f"/api/runs/{rid}/act",
                       json={"action": "forge", "card": first_uid, "branch": "sharpen"})
    assert poor.status_code == 400

    rec = service.load_run(rid)
    rec["state"]["gold"] = FORGE_COST * 2
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    ok = client.post(f"/api/runs/{rid}/act",
                     json={"action": "forge", "card": first_uid, "branch": "sharpen"})
    assert ok.status_code == 200
    assert ok.json()["run"]["gold"] == FORGE_COST
    # 重复锻造同一节点 -> 409，金币不再被扣
    dup = client.post(f"/api/runs/{rid}/act",
                      json={"action": "forge", "card": first_uid, "branch": "refine"})
    assert dup.status_code == 409
    assert client.get(f"/api/runs/{rid}").json()["gold"] == FORGE_COST
    # 该实例只记录第一次的分支
    deck = {c["uid"]: c for c in client.get(f"/api/runs/{rid}").json()["deck"]}
    assert deck[first_uid]["forges"] == ["sharpen"]


def test_forge_rejects_bad_card_or_branch(client):
    seed, path = _find_forge_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    _walk(client, rid, path)
    rec = service.load_run(rid)
    rec["state"]["gold"] = 100
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    bad_card = client.post(f"/api/runs/{rid}/act",
                           json={"action": "forge", "card": "nope", "branch": "sharpen"})
    assert bad_card.status_code == 400
    uid = service.load_run(rid)["state"]["deck"][0]
    bad_branch = client.post(f"/api/runs/{rid}/act",
                             json={"action": "forge", "card": uid, "branch": "overload"})
    assert bad_branch.status_code == 400
    # 失败请求不扣款
    assert service.load_run(rid)["state"]["gold"] == 100


# ---------- 同名卡独立成长 ----------
def test_same_name_cards_grow_independently(client):
    seed, path = _find_forge_path()
    created = client.post("/api/runs", json={"seed": seed}).json()
    rid = created["run_id"]
    strikes = [c for c in created["deck"] if c["id"] == "strike"]
    assert len(strikes) >= 2  # 初始牌组有 4 张打击，各持独立 uid
    _walk(client, rid, path)
    rec = service.load_run(rid)
    rec["state"]["gold"] = 100
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    target, other = strikes[0]["uid"], strikes[1]["uid"]
    r = client.post(f"/api/runs/{rid}/act",
                    json={"action": "forge", "card": target, "branch": "sharpen"})
    assert r.status_code == 200
    deck = {c["uid"]: c for c in r.json()["run"]["deck"]}
    assert deck[target]["forges"] == ["sharpen"]
    assert deck[other]["forges"] == []


# ---------- 贯通战斗结算 ----------
def test_forge_takes_effect_in_next_battle(client):
    seed, epath = _find_enemy_path(0)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    # 在开战前直接给 c1（打击）写入锻造：锋锐 + 精炼
    rec = service.load_run(rid)
    rec["state"]["card_instances"]["c1"]["forges"] = ["sharpen", "refine"]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    run = _walk(client, rid, epath)
    assert run["in_battle"] is True
    # 把 c1 精确放进手牌（确定性，不依赖洗牌）并压残敌人
    rec = service.load_run(rid)
    b = rec["state"]["battle"]
    b["hand"] = ["c1"]
    b["draw_pile"] = [u for u in b["draw_pile"] if u != "c1"]
    b["discard"] = [u for u in b["discard"] if u != "c1"]
    b["energy"], b["max_energy"] = 3, 3
    b["entities"]["enemy"]["hp"] = 30
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    view = client.get(f"/api/runs/{rid}").json()
    hand = view["battle"]["hand"]
    assert hand[0]["uid"] == "c1" and hand[0]["cost"] == 0  # 精炼后 0 费
    res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": "c1"})
    assert res.status_code == 200
    dmg = [e["value"] for e in res.json()["log"]
           if isinstance(e, dict) and e.get("action") == "damage"]
    assert dmg == [9]  # 6 基础 + 3 锋锐


# ---------- 贯通续局 ----------
def test_forge_persists_across_resume(client):
    seed, path = _find_forge_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    uid = run["deck"][0]["uid"]
    rec = service.load_run(rid)
    rec["state"]["gold"] = 100
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    client.post(f"/api/runs/{rid}/act",
                json={"action": "forge", "card": uid, "branch": "empower"})
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    by_uid = {c["uid"]: c for c in resumed["deck"]}
    assert by_uid[uid]["forges"] == ["empower"]
    assert resumed["forge_available"] is False


# ---------- 贯通回放 ----------
def test_forge_recorded_in_replay(client):
    seed, path = _find_forge_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    uid = run["deck"][0]["uid"]
    rec = service.load_run(rid)
    rec["state"]["gold"] = 100
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    client.post(f"/api/runs/{rid}/act",
                json={"action": "forge", "card": uid, "branch": "refine"})
    replay = client.get(f"/api/runs/{rid}/replay").json()
    forged = [a for a in replay["actions"] if a["action"] == "forge"]
    assert len(forged) == 1
    payload = forged[0]["payload"]
    assert payload["card"] == uid and payload["branch"] == "refine"
    assert payload["node"] is None and payload["option"] is None


# ---------- 奖励入牌：新实例独立成长 ----------
def test_battle_reward_add_card_creates_independent_instance(client):
    seed, epath = _find_enemy_path(0)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    _walk(client, rid, epath)
    rec = service.load_run(rid)
    enemy_id = rec["state"]["battle"]["enemy"]
    # 压残敌人 + 手里塞打击，一击获胜
    b = rec["state"]["battle"]
    b["entities"]["enemy"]["hp"] = 1
    b["hand"] = ["c1"]
    b["energy"] = 3
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    res = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": "c1"})
    assert res.json()["run"]["status"] == "in_progress"
    opts = res.json()["run"]["reward_options"]
    card_idx = next(i for i, o in enumerate(opts) if o["kind"] == "card")
    expected_cid = next(e["card"] for e in opts[card_idx]["effects"] if e["type"] == "add_card")
    claimed = client.post(f"/api/runs/{rid}/act",
                          json={"action": "claim_reward", "option": card_idx})
    assert claimed.status_code == 200
    state = service.load_run(rid)["state"]
    new_uid = state["deck"][-1]
    assert state["card_instances"][new_uid] == {"id": expected_cid, "forges": []}
    # 新卡确实来自敌人掉落表，且 uid 不与旧实例撞号
    from app.enemies import get_enemy
    assert expected_cid in get_enemy(enemy_id)["reward_cards"]
    assert state["next_card_seq"] == len(state["card_instances"]) + 1
    # 新实例与同名旧卡（若有）成长状态互相独立
    same_name = [u for u, i in state["card_instances"].items() if i["id"] == expected_cid]
    assert new_uid in same_name and all(state["card_instances"][u]["forges"] == [] for u in same_name)


# ---------- 旧档兼容 ----------
def test_legacy_save_migrates_mid_battle_and_plays(client):
    seed, epath = _find_enemy_path(0)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    _walk(client, rid, epath)
    # 把存档退回旧版本形态：裸 id 牌堆/牌组，无实例表
    rec = db.load_run(rid)
    st = rec["state"]
    for pile in ("draw_pile", "hand", "discard"):
        st["battle"][pile] = [st["card_instances"][u]["id"] for u in st["battle"][pile]]
    st["battle"].pop("card_instances", None)
    st["deck"] = [inst["id"] for inst in st["card_instances"].values()]
    del st["card_instances"]
    del st["next_card_seq"]
    st.pop("forge_claimed", None)
    db.save_run(rid, st["status"], st["position"], st)

    resumed = client.get(f"/api/runs/{rid}/resume").json()
    # 牌组与手牌都迁移为实例结构
    assert all(isinstance(c, dict) and c["uid"] for c in resumed["deck"])
    assert all(isinstance(h, dict) and h["uid"] for h in resumed["battle"]["hand"])
    # 同名卡获得不同 uid，且战斗三堆合计仍是整副牌
    st2 = service.load_run(rid)["state"]
    piles = st2["battle"]["draw_pile"] + st2["battle"]["hand"] + st2["battle"]["discard"]
    assert sorted(piles) == sorted(st2["deck"])
    assert all(u in st2["card_instances"] for u in piles)
    # 续局后可正常打牌
    uid = resumed["battle"]["hand"][0]["uid"]
    play = client.post(f"/api/runs/{rid}/act", json={"action": "play", "card": uid})
    assert play.status_code == 200


def test_legacy_save_outside_battle_migrates(client):
    # 旧档停在奖励节点（无战斗结构）也能迁移
    rid = client.post("/api/runs", json={"seed": 1}).json()["run_id"]
    rec = db.load_run(rid)
    st = rec["state"]
    st["deck"] = [inst["id"] for inst in st["card_instances"].values()]
    del st["card_instances"]
    del st["next_card_seq"]
    db.save_run(rid, st["status"], st["position"], st)
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert len(resumed["deck"]) == 7
    assert len({c["uid"] for c in resumed["deck"]}) == 7
    assert all(c["forges"] == [] for c in resumed["deck"])
    assert resumed["forge_claimed"] is True
