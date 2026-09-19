"""原子提交与并发一致性：

- 同一局并发行动被进程内串行锁 + revision 乐观锁串行化，存档/日志绝不分叉
- req_id 幂等令牌：重复请求回放首次结果，不重复生效/扣款
- 存档与动作日志原子提交：提交中途异常整体回滚（无存档无日志无解锁）
- 战败解锁奖励与存档/日志同事务
- runs.revision 每次成功提交 +1，跨进程写入冲突抛 409 而非静默覆盖
- 异常（损坏）日志不拖垮整段回放：标记 error 帧，其余步骤照常重建
- 旧库平滑升级：runs.revision 列自动补齐
"""
import json
import threading

import pytest

from app import db, service, mapgen


def _find_enemy_path(seed_start=0):
    for seed in range(seed_start, seed_start + 400):
        m = mapgen.generate_map(seed)
        for n in m["routes"][m["start"]]:
            if m["nodes"][n]["type"] in (mapgen.ENCOUNTER, mapgen.ELITE):
                return seed, n
    raise AssertionError("no enemy node")


def _set_player_hp(rid, hp):
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["player"]["hp"] = hp
    rec["state"]["health"] = hp
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])


def _max_seq(rid):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT MAX(seq) m FROM battle_events WHERE run_id=?", (rid,)).fetchone()
        return row["m"]
    finally:
        conn.close()


# ---------- 并发：同一局并行行动不丢更新、不产生序号空洞 ----------
def test_concurrent_actions_are_serialized_with_consistent_state_and_log(client):
    seed, node = _find_enemy_path()
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    assert client.post(f"/api/runs/{rid}/act",
                       json={"action": "choose_node", "node": node}).status_code == 200

    barrier = threading.Barrier(2)
    results = []

    def fire():
        barrier.wait()
        r = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        results.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 至少第一次成功；第二次要么 409（状态冲突/非玩家回合），要么顺序成功——
    # 关键是存档与日志必须一致
    assert 200 in results
    events = db.load_events(rid)
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1))  # 无空洞

    rec = service.load_run(rid)
    # 回放逐帧校验：存档与日志一致时不应出现 mismatch/error
    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["verification"]["mismatch"] == 0
    assert rep["verification"]["error"] == 0
    # 最后一帧的位置/状态与存档一致
    assert rep["final_view"]["position"] == rec["state"]["position"]
    assert rep["final_view"]["status"] == rec["state"]["status"]


# ---------- 幂等：相同 req_id 重复请求只生效一次 ----------
def test_duplicate_request_with_same_req_id_is_idempotent(client):
    seed, node = _find_enemy_path(0)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})

    r1 = client.post(f"/api/runs/{rid}/act",
                     json={"action": "end_turn", "req_id": "tok-1"})
    assert r1.status_code == 200
    seq1 = r1.json()["seq"]

    r2 = client.post(f"/api/runs/{rid}/act",
                     json={"action": "end_turn", "req_id": "tok-1"})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["duplicate"] is True
    assert body2["seq"] == seq1  # 回放首次响应，未再产生新事件

    events = db.load_events(rid)
    assert [e["action"] for e in events].count("end_turn") == 1
    assert events[-1]["seq"] == seq1


def test_idempotent_token_scoped_per_run_does_not_collide(client):
    rid1 = client.post("/api/runs", json={"seed": 1}).json()["run_id"]
    rid2 = client.post("/api/runs", json={"seed": 2}).json()["run_id"]
    # 同一令牌在不同 run 各自生效
    for rid in (rid1, rid2):
        node = next(n for n in service.load_run(rid)["map"]["routes"]["start"])
        client.post(f"/api/runs/{rid}/act",
                    json={"action": "choose_node", "node": node, "req_id": "shared-token"})
    assert client.get(f"/api/runs/{rid1}/replay").json()["verification"]["error"] == 0
    assert client.get(f"/api/runs/{rid2}/replay").json()["verification"]["error"] == 0


def test_idempotent_rejected_request_is_not_recorded(client):
    """校验失败的请求不写入幂等表：同一令牌修正参数后仍可成功。"""
    seed, node = _find_enemy_path(100)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    bad = client.post(f"/api/runs/{rid}/act",
                      json={"action": "choose_node", "node": "no-such-node", "req_id": "tok-x"})
    assert bad.status_code == 400
    good = client.post(f"/api/runs/{rid}/act",
                       json={"action": "choose_node", "node": node, "req_id": "tok-x"})
    assert good.status_code == 200


# ---------- 原子提交：写日志失败时存档与解锁整体回滚 ----------
def test_act_atomic_rollback_when_event_insert_fails(client, monkeypatch):
    seed, node = _find_enemy_path(200)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    _set_player_hp(rid, 1)
    profile_before = db.get_profile()
    state_before = service.load_run(rid)["state"]
    seq_before = _max_seq(rid)

    # 让“动作日志追加”在事务内抛错：模拟写入失败（磁盘异常等）
    def boom(self, run_id, seq, action, payload):
        raise OSError("simulated write failure")

    monkeypatch.setattr(db.Transaction, "insert_event", boom)

    with pytest.raises(OSError):
        service.act(rid, {"action": "end_turn"})

    rec = service.load_run(rid)
    # 存档完全回退：战斗仍在进行、玩家仍是 1 血、状态未变 lost
    assert rec["state"]["status"] == "in_progress"
    assert rec["state"]["in_battle"] is True
    assert rec["state"]["health"] == 1
    # 没有追加半截日志
    assert _max_seq(rid) == seq_before
    # 战败解锁也随事务回滚（profile 未变）
    assert db.get_profile() == profile_before
    assert state_before["status"] == "in_progress"


def test_loss_unlock_commits_together_with_state_and_log(client):
    seed, node = _find_enemy_path(300)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    unlocked_before = len(client.get(f"/api/runs/{rid}").json()["unlocked_cards"]["unlocked"])
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    _set_player_hp(rid, 1)

    # 敌人意图可能有非伤害回合：连续结束回合直到玩家被击败
    res = None
    for _ in range(10):
        res = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
        assert res.status_code == 200
        if res.json()["run"]["status"] == "lost":
            break
        _set_player_hp(rid, 1)
    assert res.json()["run"]["status"] == "lost"

    # 存档、日志、profile 解锁都已提交且回放一致
    rec = service.load_run(rid)
    assert rec["state"]["status"] == "lost"
    unlocked_after = client.get(f"/api/runs/{rid}").json()["unlocked_cards"]["unlocked"]
    assert len(unlocked_after) == unlocked_before + 1
    rep = client.get(f"/api/runs/{rid}/replay").json()
    # 测试中途手工压血造成的 mismatch 属预期（直接改了存档而非走动作）；
    # 关键是日志里确实记录了战败动作，且没有损坏到无法解析（无 error 帧）
    assert rep["verification"]["error"] == 0
    assert any(a["action"] == "end_turn" for a in rep["actions"])

    # 终局后重复行动 -> 400 状态冲突，不产生任何写入
    seq = _max_seq(rid)
    again = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    assert again.status_code == 400
    assert _max_seq(rid) == seq


# ---------- 乐观锁 revision ----------
def test_revision_increments_per_successful_commit(client):
    rid = client.post("/api/runs", json={"seed": 11}).json()["run_id"]
    assert service.load_run(rid)["revision"] == 1
    node = next(n for n in service.load_run(rid)["map"]["routes"]["start"])
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    assert service.load_run(rid)["revision"] == 2
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    assert service.load_run(rid)["revision"] == 3


def test_stale_revision_conflict_raises_stateconflict_and_preserves_newer_state(client):
    """跨进程式并发：拿着旧 revision 提交 -> StateConflict，新状态不被覆盖。"""
    seed, node = _find_enemy_path(400)
    rid = service.create_run(seed=seed)["run_id"]

    # 第一个请求读档后、提交前，另一连接先把存档推进
    rec = db.load_run(rid)
    stale = rec["revision"]
    service.act(rid, {"action": "choose_node", "node": node})
    newer = service.load_run(rid)
    assert newer["revision"] > stale

    # 用旧快照 CAS：必须失败（db.Conflict），且不影响已提交的新状态
    with pytest.raises(db.Conflict):
        with db.transaction() as t:
            t.save_run_cas(rid, rec["state"]["status"], rec["state"]["position"],
                          rec["state"], stale)
    # 存档仍是先提交者的结果
    assert service.load_run(rid)["state"]["position"] == node


# ---------- 异常日志：损坏 payload 不拖垮整段回放 ----------
def test_replay_survives_corrupt_event_payload(client):
    seed, node = _find_enemy_path(500)
    rid = client.post("/api/runs", json={"seed": seed}).json()["run_id"]
    client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node})
    client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})

    # 直接把某条日志的 payload_json 改成非法 JSON（模拟损坏行）
    conn = db.get_conn()
    try:
        conn.execute("UPDATE battle_events SET payload_json='{not json' WHERE run_id=? AND seq=2",
                     (rid,))
        conn.commit()
    finally:
        conn.close()

    rep = client.get(f"/api/runs/{rid}/replay").json()
    bad = next(s for s in rep["steps"] if s["seq"] == 2)
    assert bad["check"] == "error"
    assert "CorruptEventError" in bad["error"]
    assert rep["verification"]["error"] >= 1
    # 损坏步之前的帧（建局）依然正常，且时间轴完整保留，前端可跳转其余帧
    assert rep["steps"][0]["check"] == "ok"
    assert [s["seq"] for s in rep["steps"]] == [1, 2, 3]
    assert rep["steps"][-1]["view"]


# ---------- 旧库升级：revision 列自动补齐 ----------
def test_legacy_schema_without_revision_upgrades_on_init(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy.db")
    # DB_PATH 在模块导入时已固定，这里同时重定向模块常量
    monkeypatch.setenv("GAME_DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    import sqlite3
    # 手工建一个没有 revision 列的旧结构库
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, seed INTEGER, status TEXT, position TEXT, "
        "map_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE battle_events (run_id TEXT, seq INTEGER, action TEXT, "
                 "payload_json TEXT, PRIMARY KEY(run_id, seq))")
    conn.execute("CREATE TABLE profile (id TEXT PRIMARY KEY, unlocked_cards TEXT)")
    conn.execute("INSERT INTO runs VALUES ('r1', 1, 'in_progress', 'start', '{}', '{}', '', '')")
    conn.commit()
    conn.close()

    db.init_db()
    rec = db.load_run("r1")
    assert rec["revision"] == 1
    # 升级后可正常写
    db.update_run_status("r1", "lost")
    assert db.load_run("r1")["status"] == "lost"
