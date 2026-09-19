"""旅途商店：商人节点库存、购买卡牌/遗物、付费移除指定卡牌实例；
统一扣款/售罄/失败回退；交易贯通后续战斗；续局与回放一致；库存确定性。"""
import copy

import pytest

from app import service, mapgen, db, shop as shop_mod


# ---------- 纯函数：库存生成确定性 ----------
def test_stock_deterministic_and_fresh_each_seed():
    owned_cards, owned_relics = set(), set()
    a = shop_mod.generate_stock(123, owned_cards, owned_relics)
    b = shop_mod.generate_stock(123, owned_cards, owned_relics)
    assert a == b
    c = shop_mod.generate_stock(456, owned_cards, owned_relics)
    assert [(x["sku"], x["price"]) for x in c["cards"]] != [(x["sku"], x["price"]) for x in a["cards"]] \
        or [x["sku"] for x in c["relics"]] != [x["sku"] for x in a["relics"]]


def test_stock_excludes_owned():
    stock = shop_mod.generate_stock(1, {"cleave"}, {"anvil"})
    assert all(it["card"] != "cleave" for it in stock["cards"])
    assert all(it["relic"] != "anvil" for it in stock["relics"])
    # 货架项都带独立 sku、售价与未售标记
    for it in stock["cards"] + stock["relics"]:
        assert it["sku"] and it["price"] > 0 and it["sold"] is False


def test_remove_cost_grows_per_use():
    assert shop_mod.next_remove_cost(0) == shop_mod.REMOVE_BASE_COST
    assert shop_mod.next_remove_cost(1) == shop_mod.REMOVE_BASE_COST + shop_mod.REMOVE_COST_GROWTH


# ---------- 地图 ----------
def _find_shop_path(seed_start=0):
    """返回 (seed, 到商店节点的完整路径)，最多探两行。"""
    for seed in range(seed_start, seed_start + 2000):
        m = mapgen.generate_map(seed)
        for n0 in m["routes"][m["start"]]:
            if m["nodes"][n0]["type"] == mapgen.SHOP:
                return seed, [n0]
            for n1 in m["routes"][n0]:
                if m["nodes"][n1]["type"] == mapgen.SHOP:
                    return seed, [n0, n1]
    raise AssertionError("no shop node")


def _walk(client, rid, nodes):
    run = None
    for n in nodes:
        run = client.post(f"/api/runs/{rid}/act",
                          json={"action": "choose_node", "node": n}).json()["run"]
    return run


def _set_gold(rid, gold):
    rec = service.load_run(rid)
    rec["state"]["gold"] = gold
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])


def test_shop_appears_on_map_but_not_first_row():
    seed, _ = _find_shop_path()
    m = mapgen.generate_map(seed)
    assert any(n["type"] == mapgen.SHOP for n in m["nodes"].values())
    assert all(m["nodes"][n]["type"] != mapgen.SHOP for n in m["routes"][m["start"]])


# ---------- 进入商店 ----------
def test_enter_shop_builds_stock_and_resumes_identical(client):
    seed, path = _find_shop_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    assert run["shop_available"] is True
    shop = run["shop"]
    assert 1 <= len(shop["cards"]) <= shop_mod.CARD_OFFER_COUNT
    assert 1 <= len(shop["relics"]) <= shop_mod.RELIC_OFFER_COUNT
    assert shop["remove"]["cost"] == shop_mod.REMOVE_BASE_COST
    assert shop["tx"] == []
    # 续局：库存（含货架与价格）完全一致
    resumed = client.get(f"/api/runs/{rid}/resume").json()
    assert resumed["shop"] == shop


def test_same_seed_same_shop_stock(client):
    seed, path = _find_shop_path()
    r1 = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    r2 = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    a = _walk(client, r1, path)["shop"]
    b = _walk(client, r2, path)["shop"]
    assert [(c["sku"], c["price"]) for c in a["cards"]] == [(c["sku"], c["price"]) for c in b["cards"]]
    assert [(c["sku"], c["price"]) for c in a["relics"]] == [(c["sku"], c["price"]) for c in b["relics"]]


# ---------- 购买卡牌：扣款、售罄、新实例 ----------
def test_buy_card_deducts_sells_out_and_appends_instance(client):
    seed, path = _find_shop_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    item = run["shop"]["cards"][0]

    # 金币不足 -> 400 且无副作用
    poor = client.post(f"/api/runs/{rid}/act",
                       json={"action": "shop_buy", "kind": "card", "sku": item["sku"]})
    assert poor.status_code == 400
    st = service.load_run(rid)["state"]
    assert st["gold"] == 0 and len(st["deck"]) == 7
    assert next(c for c in st["shop"]["cards"] if c["sku"] == item["sku"])["sold"] is False

    _set_gold(rid, 200)
    ok = client.post(f"/api/runs/{rid}/act",
                     json={"action": "shop_buy", "kind": "card", "sku": item["sku"]})
    assert ok.status_code == 200
    tx = ok.json()["log"][0]["shop_tx"]
    assert tx == {"type": "buy", "kind": "card", "sku": item["sku"],
                  "price": item["price"], "gold_left": 200 - item["price"],
                  "uid": tx["uid"]}
    assert ok.json()["run"]["gold"] == 200 - item["price"]

    st = service.load_run(rid)["state"]
    new_uid = st["deck"][-1]
    assert st["card_instances"][new_uid] == {"id": item["card"], "forges": []}
    assert st["next_card_seq"] == len(st["card_instances"]) + 1
    assert next(c for c in st["shop"]["cards"] if c["sku"] == item["sku"])["sold"] is True

    # 重复购买同一货架项 -> 409 售罄，不再扣款
    dup = client.post(f"/api/runs/{rid}/act",
                      json={"action": "shop_buy", "kind": "card", "sku": item["sku"]})
    assert dup.status_code == 409
    assert client.get(f"/api/runs/{rid}").json()["gold"] == 200 - item["price"]


def test_buy_rejects_bad_shelf_without_side_effects(client):
    seed, path = _find_shop_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    _walk(client, rid, path)
    _set_gold(rid, 100)
    bad_kind = client.post(f"/api/runs/{rid}/act",
                           json={"action": "shop_buy", "kind": "potion", "sku": "x"})
    bad_sku = client.post(f"/api/runs/{rid}/act",
                          json={"action": "shop_buy", "kind": "card", "sku": "card:nope"})
    assert bad_kind.status_code == 400 and bad_sku.status_code == 400
    # 失败不扣款、库存不变
    assert service.load_run(rid)["state"]["gold"] == 100
    assert all(not c["sold"] for c in service.load_run(rid)["state"]["shop"]["cards"])


# ---------- 购买遗物：影响后续战斗 ----------
def test_buy_relic_applies_effect_in_next_battle(client):
    seed, path = _find_shop_path(300)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    anvil = next((r for r in run["shop"]["relics"] if r["relic"] == "anvil"), None)
    if anvil is None:
        pytest.skip("该种子商店未刷新铁砧")
    _set_gold(rid, 200)
    ok = client.post(f"/api/runs/{rid}/act",
                     json={"action": "shop_buy", "kind": "relic", "sku": anvil["sku"]})
    assert ok.status_code == 200
    assert "power_up" in ok.json()["run"]["relics"]
    # 遗物货架项标记售罄，再次购买 409
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "shop_buy", "kind": "relic", "sku": anvil["sku"]}).status_code == 409

    # 离开商店进入战斗：铁磐的 power_up 幻化为力量 +2；买来的卡进三堆
    m = service.load_run(rid)["map"]
    enemy_node = next(n for n in m["routes"][path[-1]]
                      if m["nodes"][n]["type"] in (mapgen.ENCOUNTER, mapgen.ELITE))
    battle_view = client.post(f"/api/runs/{rid}/act",
                              json={"action": "choose_node", "node": enemy_node}).json()["run"]
    assert battle_view["shop"] is None and battle_view["shop_available"] is False
    strength = next(s for s in battle_view["battle"]["player"]["statuses"] if s["id"] == "strength")
    assert strength["value"] == 2


# ---------- 付费移除指定卡牌实例 ----------
def test_remove_card_instance_deducts_grows_price_and_guards_deck(client):
    seed, path = _find_shop_path(500)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    first = run["deck"][0]
    _set_gold(rid, 500)

    ok = client.post(f"/api/runs/{rid}/act",
                     json={"action": "shop_remove", "card": first["uid"]})
    assert ok.status_code == 200
    tx = ok.json()["log"][0]["shop_tx"]
    assert tx["type"] == "remove" and tx["price"] == shop_mod.REMOVE_BASE_COST
    assert tx["gold_left"] == 500 - shop_mod.REMOVE_BASE_COST
    st = service.load_run(rid)["state"]
    assert first["uid"] not in st["deck"] and first["uid"] not in st["card_instances"]
    assert len(st["deck"]) == 6
    assert st["shop"]["remove"]["used"] == 1
    assert st["shop"]["remove"]["cost"] == shop_mod.REMOVE_BASE_COST + shop_mod.REMOVE_COST_GROWTH

    # 重复移除同一实例（已不存在）-> 400 不扣款
    dup = client.post(f"/api/runs/{rid}/act",
                      json={"action": "shop_remove", "card": first["uid"]})
    assert dup.status_code == 400
    assert service.load_run(rid)["state"]["gold"] == 500 - shop_mod.REMOVE_BASE_COST

    # 第二次移除按递增价扣款
    second = client.get(f"/api/runs/{rid}").json()["deck"][0]["uid"]
    r2 = client.post(f"/api/runs/{rid}/act",
                     json={"action": "shop_remove", "card": second})
    assert r2.json()["log"][0]["shop_tx"]["price"] == shop_mod.REMOVE_BASE_COST + shop_mod.REMOVE_COST_GROWTH


def test_remove_rejects_unknown_uid_outside_shop_and_min_deck(client):
    seed, path = _find_shop_path(700)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    _walk(client, rid, path)
    _set_gold(rid, 100)
    # 未知 uid
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "shop_remove", "card": "nope"}).status_code == 400

    # 牌组砍到 1 张：不允许再移除，且不扣款
    rec = service.load_run(rid)
    keep = rec["state"]["deck"][0]
    for uid in list(rec["state"]["deck"]):
        if uid != keep:
            rec["state"]["deck"].remove(uid)
            del rec["state"]["card_instances"][uid]
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "shop_remove", "card": keep}).status_code == 400
    assert service.load_run(rid)["state"]["gold"] == 100

    # 商店外不能交易（新局停在起点）
    other = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    assert client.post(f"/api/runs/{other}/act",
                       json={"action": "shop_remove", "card": "c1"}).status_code == 400
    assert client.post(f"/api/runs/{other}/act",
                       json={"action": "shop_buy", "kind": "card", "sku": "card:strike"}).status_code == 400


# ---------- 失败回退契约 ----------
def test_commit_shop_tx_rolls_back_entire_run_on_failure():
    seed, path = _find_shop_path(1300)
    rid = service.create_run(seed=seed)["run_id"]
    for n in path:
        service.act(rid, {"action": "choose_node", "node": n})
    rec = service.load_run(rid)
    rec["state"]["gold"] = 100
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    run = service.load_run(rid)["state"]
    snapshot = copy.deepcopy(run)

    def mutate(r):
        r["gold"] -= 10  # 先扣款
        r["deck"].pop()  # 再改牌组
        raise RuntimeError("boom mid-transaction")

    with pytest.raises(RuntimeError):
        service._commit_shop_tx(run, mutate, lambda res: {"type": "x"})
    # 扣款与牌组变更全部回退，交易记录不写入
    assert run == snapshot
    assert run["shop"]["tx"] == []


# ---------- 贯通回放 ----------
def test_shop_actions_recorded_in_replay(client):
    seed, path = _find_shop_path(1600)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    run = _walk(client, rid, path)
    _set_gold(rid, 300)
    card_sku = run["shop"]["cards"][0]["sku"]
    remove_uid = client.get(f"/api/runs/{rid}").json()["deck"][0]["uid"]
    client.post(f"/api/runs/{rid}/act",
                json={"action": "shop_buy", "kind": "card", "sku": card_sku})
    client.post(f"/api/runs/{rid}/act",
                json={"action": "shop_remove", "card": remove_uid})

    replay = client.get(f"/api/runs/{rid}/replay").json()
    buy = [a for a in replay["actions"] if a["action"] == "shop_buy"]
    rem = [a for a in replay["actions"] if a["action"] == "shop_remove"]
    assert len(buy) == 1 and buy[0]["payload"]["sku"] == card_sku
    assert len(rem) == 1 and rem[0]["payload"]["card"] == remove_uid
    # 确定性：重复取回放完全一致
    assert client.get(f"/api/runs/{rid}/replay").json()["actions"] == replay["actions"]
