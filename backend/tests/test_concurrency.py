"""并发一致性与原子提交：

- 行动（校验/存档/日志/解锁）单事务原子提交：写入中途失败整体回滚，无部分写入；
- per-run 锁串行化同一局：并发重复领奖/锻造只有一个生效，金币只扣一次；
- 请求幂等键 request_id：重复/并发同键返回首次结果，不重复执行；
- 乐观版本 expected_rev：基于过期视口提交 -> 409 状态冲突；
- 战败解锁与存档/日志同事务：解锁写失败回滚整步，并发战败只解锁一次；
- 异常日志（载荷损坏/序号缺口）不拖垮回放，明确标注 error/gap。
"""
import threading

import pytest

from app import db, mapgen, service


# ---------- 工具 ----------
def _find_node(ntype, seed_start=0, span=800):
    for seed in range(seed_start, seed_start + span):
        m = mapgen.generate_map(seed)
        for n in m["routes"][m["start"]]:
            if m["nodes"][n]["type"] == ntype:
                return seed, n
    pytest.skip(f"未找到 {ntype} 节点")


def _gather(fn, n):
    out = []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()  # 尽量让所有线程同时撞进来，放大竞态
        out.append(fn())

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def _replay_verification(client, rid):
    return client.get(f"/api/runs/{rid}/replay").json()["verification"]


# ---------- 原子提交：中途写入失败整体回滚 ----------
def test_act_atomic_rollback_when_event_write_fails(client):
    rid = client.post("/api/runs", json={"seed": 4242}).json()["run_id"]
    m = service.load_run(rid)["map"]
    node = next(n for n in m["routes"][m["start"]] if m["nodes"][n]["type"] == mapgen.ENCOUNTER)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})

    events_before = db.load_events(rid)
    state_ckpt_before = service.state_checkpoint(service.load_run(rid)["state"])

    orig = db.append_event_conn
    def boom(conn, run_id, seq, action, payload):
        raise OSError("simulated write failure")
    db.append_event_conn = boom
    try:
        with pytest.raises(OSError):
            service.act(rid, {"action": "end_turn"})
    finally:
        db.append_event_conn = orig

    # 存档与日志都停留在失败前：无“存档动了、日志缺行”的分叉
    assert db.load_events(rid) == events_before
    assert service.state_checkpoint(service.load_run(rid)["state"]) == state_ckpt_before

    # 恢复后同一动作可正常提交，回放逐位一致
    r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    assert r.status_code == 200
    v = _replay_verification(client, rid)
    assert v["mismatch"] == 0 and v["error"] == 0


def test_create_run_atomic_when_event_write_fails(client, monkeypatch):
    def boom(conn, run_id, seq, action, payload):
        raise OSError("create event fail")
    monkeypatch.setattr(db, "append_event_conn", boom)
    with pytest.raises(OSError):
        service.create_run(seed=123)
    monkeypatch.undo()
    # 建局事件写失败：不留“没有首条日志的孤儿存档”
    conn = db.get_conn()
    n_runs = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    conn.close()
    assert n_runs == 0
    # 新建一个不同 seed 的局仍正常（失败没有污染连接/事务状态）
    ok = client.post("/api/runs", json={"seed": 456})
    assert ok.status_code == 200
    rid = ok.json()["run_id"]
    assert [e["action"] for e in db.load_events(rid)] == ["create"]


def test_loss_unlock_rolls_back_when_profile_write_fails(client, monkeypatch):
    seed, node = _find_node(mapgen.ENCOUNTER)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["player"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    events_before = db.load_events(rid)

    def boom(conn, unlocked_cards):
        raise OSError("profile disk fail")
    monkeypatch.setattr(db, "upsert_profile_conn", boom)

    # 敌人可能不立即造成伤害：循环到出现致命伤害为止（每次失败都必须回滚）
    lost_attempt = False
    for _ in range(8):
        with pytest.raises(OSError):
            service.act(rid, {"action": "end_turn"})
        st = service.load_run(rid)["state"]
        # 关键：即使这一步本应判负并发解锁，profile 失败也必须回滚整步
        assert st["status"] == "in_progress"
        assert st["in_battle"] is True
        assert db.load_events(rid) == events_before
        lost_attempt = True
    assert lost_attempt


# ---------- 并发重复：只生效一次 ----------
def test_concurrent_duplicate_reward_claim_applies_once(client):
    seed, node = _find_node(mapgen.REWARD)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})

    codes = _gather(
        lambda: client.post(f"/api/runs/{rid}/act",
                            json={"action": "claim_reward", "option": 0}).status_code,
        8,
    )
    assert codes.count(200) == 1
    assert codes.count(409) == 7
    assert [e["action"] for e in db.load_events(rid)].count("claim_reward") == 1
    v = _replay_verification(client, rid)
    assert v["mismatch"] == 0 and v["error"] == 0


def test_concurrent_forge_charges_gold_once(client):
    seed, node = _find_node(mapgen.FORGE)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    rec = service.load_run(rid)
    rec["state"]["gold"] = 100
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    uid = service.load_run(rid)["state"]["deck"][0]

    codes = _gather(
        lambda: client.post(f"/api/runs/{rid}/act",
                            json={"action": "forge", "card": uid, "branch": "sharpen"}).status_code,
        6,
    )
    assert codes.count(200) == 1 and codes.count(409) == 5
    st = service.load_run(rid)["state"]
    assert st["gold"] == 100 - service.FORGE_COST  # 只扣一次
    assert st["card_instances"][uid]["forges"] == ["sharpen"]
    # 存档与日志一致（直接改库加金属于测试夹具动作，不参与回放逐位校验；
    # 校验每个真实提交动作内部自洽：无 error、无悬空状态）
    assert [e["action"] for e in db.load_events(rid)][-1] == "forge"


def test_concurrent_loss_unlocks_only_once(client):
    seed, node = _find_node(mapgen.ENCOUNTER)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["player"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])

    def end_turn():
        return client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"}).status_code

    codes = _gather(end_turn, 4)
    # 恰好一个请求推进到战败；其余在锁后看到 run 已结束 -> 400
    assert codes.count(200) == 1
    assert all(c in (200, 400) for c in codes)
    st = service.load_run(rid)["state"]
    assert st["status"] == "lost"
    # profile 只解锁一次：再发行动不会再次解锁
    unlocked_now = list(db.get_profile()["unlocked"])
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})  # 已结束 -> 400
    assert list(db.get_profile()["unlocked"]) == unlocked_now


# ---------- 请求级幂等 ----------
def test_same_request_id_returns_first_response_without_double_effect(client):
    seed, node = _find_node(mapgen.REWARD)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})

    body = {"action": "claim_reward", "option": 0, "request_id": "req-1"}
    first = client.post(f"/api/runs/{rid}/act", json=body)
    second = client.post(f"/api/runs/{rid}/act", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["seq"] == second.json()["seq"]
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert [e["action"] for e in db.load_events(rid)].count("claim_reward") == 1


def test_concurrent_same_request_id_executes_once(client):
    seed, node = _find_node(mapgen.REWARD)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})

    resp = _gather(
        lambda: client.post(f"/api/runs/{rid}/act",
                            json={"action": "claim_reward", "option": 0,
                                  "request_id": "race-id"}).json(),
        8,
    )
    assert len({r["seq"] for r in resp}) == 1
    assert [r["duplicate"] for r in resp].count(False) == 1
    assert [r["duplicate"] for r in resp].count(True) == 7
    assert [e["action"] for e in db.load_events(rid)].count("claim_reward") == 1


def test_different_request_ids_still_hit_business_idempotency(client):
    """不带/带不同 request_id 的重复领奖：由业务幂等键拦截为 409，不重复发奖。"""
    seed, node = _find_node(mapgen.REWARD)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "claim_reward", "option": 0,
                             "request_id": "a"}).status_code == 200
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "claim_reward", "option": 0,
                             "request_id": "b"}).status_code == 409
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "claim_reward", "option": 0}).status_code == 409


# ---------- 乐观版本：状态冲突 ----------
def test_stale_expected_rev_rejected_409(client):
    seed, node = _find_node(mapgen.REWARD)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    rev0 = client.get(f"/api/runs/{rid}/resume").json()["rev"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})

    stale = client.post(f"/api/runs/{rid}/act",
                        json={"action": "claim_reward", "option": 0, "expected_rev": rev0})
    assert stale.status_code == 409
    # 被拒动作无副作用：奖励仍未领取
    assert service.load_run(rid)["state"]["reward_claimed"] is False

    # 带上最新 rev 立即成功
    rev1 = client.get(f"/api/runs/{rid}/resume").json()["rev"]
    ok = client.post(f"/api/runs/{rid}/act",
                     json={"action": "claim_reward", "option": 0, "expected_rev": rev1})
    assert ok.status_code == 200
    assert ok.json()["rev"] == rev1 + 1


def test_view_and_act_carry_rev(client):
    rid = client.post("/api/runs", json={"seed": 7}).json()["run_id"]
    assert isinstance(rid, str)
    v = client.get(f"/api/runs/{rid}/resume").json()
    assert v["rev"] == 1
    # 回放帧不携带 rev（只读、不与存档版本耦合）
    assert "rev" not in client.get(f"/api/runs/{rid}/replay").json()["steps"][0]["view"]


# ---------- 旧档兼容仍走原子路径 ----------
def test_legacy_save_migration_is_atomic_with_action(client):
    """旧档（无 card_instances）首个行动：迁移+行动+日志单事务提交，回放一致。"""
    seed, node = _find_node(mapgen.ENCOUNTER)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    # 降级存档为 v1
    rec = db.load_run(rid)
    st = rec["state"]
    st["battle"]["draw_pile"] = [st["card_instances"][u]["id"] for u in st["battle"]["draw_pile"]]
    st["battle"]["hand"] = [st["card_instances"][u]["id"] for u in st["battle"]["hand"]]
    st["battle"]["discard"] = [st["card_instances"][u]["id"] for u in st["battle"]["discard"]]
    st["deck"] = [inst["id"] for inst in st["card_instances"].values()]
    del st["card_instances"]
    del st["next_card_seq"]
    db.save_run(rid, st["status"], st["position"], st)

    r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    assert r.status_code == 200
    migrated = service.load_run(rid)["state"]
    assert "card_instances" in migrated
    v = _replay_verification(client, rid)
    # 迁移步按 legacy 处理（旧结构->新结构，校验点天然不可比），无 error/mismatch
    assert v["mismatch"] == 0 and v["error"] == 0
    rep = client.get(f"/api/runs/{rid}/replay").json()
    mstep = next(s for s in rep["steps"] if s.get("migrated"))
    assert mstep["action"] == "end_turn" and mstep["check"] == "legacy"
    # 迁移事件落库时仍带规则版本（仅校验点豁免）
    assert mstep["payload"]["ver"] == service.RULES_VERSION and mstep["payload"]["migrated"] is True


# ---------- 异常日志兼容 ----------
def test_replay_survives_corrupt_event_payload(client):
    rid = client.post("/api/runs", json={"seed": 777}).json()["run_id"]
    m = service.load_run(rid)["map"]
    node = next(n for n in m["routes"][m["start"]] if m["nodes"][n]["type"] == mapgen.ENCOUNTER)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})

    # 把中间一行日志的 payload 改成截断的非法 JSON（模拟写入失败/磁盘损坏）
    conn = db.get_conn()
    conn.execute("UPDATE battle_events SET payload_json='{oops' WHERE run_id=? AND seq=2", (rid,))
    conn.commit()
    conn.close()

    rep = client.get(f"/api/runs/{rid}/replay").json()
    # 损坏行本身必为 error；其后的步骤因推演状态缺失可能继续失败，但接口不崩
    assert rep["verification"]["error"] >= 1
    bad = next(s for s in rep["steps"] if s["seq"] == 2)
    assert bad["check"] == "error" and "损坏" in bad["error"]
    # 其余步骤仍返回帧视口，整段回放可继续跳转
    assert len(rep["steps"]) == 3
    assert rep["steps"][0]["check"] == "ok"
    assert rep["steps"][-1]["view"]


def test_replay_reports_seq_gap(client):
    rid = client.post("/api/runs", json={"seed": 777}).json()["run_id"]
    m = service.load_run(rid)["map"]
    node = next(n for n in m["routes"][m["start"]] if m["nodes"][n]["type"] == mapgen.ENCOUNTER)
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    # 删掉中间行制造序号缺口（seq 1,3 仍在，缺 2），后续行仍可被读到
    conn = db.get_conn()
    conn.execute("DELETE FROM battle_events WHERE run_id=? AND seq=2", (rid,))
    conn.commit()
    conn.close()
    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert [s["seq"] for s in rep["steps"]] == [1, 3]
    assert rep["verification"]["seq_gaps"] == 1
    step = rep["steps"][-1]
    assert step["warning"] and "gap" in step["warning"]
    # 无缺口的正常局不报警告
    rid2 = client.post("/api/runs", json={"seed": 778}).json()["run_id"]
    rep2 = client.get(f"/api/runs/{rid2}/replay").json()
    assert rep2["verification"]["seq_gaps"] == 0
    assert all(s["warning"] is None for s in rep2["steps"])
