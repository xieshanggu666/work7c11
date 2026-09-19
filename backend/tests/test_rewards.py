"""防重复领奖 & 失败解锁新卡。"""
import pytest

from app import service, mapgen, db


def _find_run_with_row0_of_type(seed_start, ntype):
    for seed in range(seed_start, seed_start + 200):
        m = mapgen.generate_map(seed)
        row0 = m["routes"][m["start"]]
        for n in row0:
            if m["nodes"][n]["type"] == ntype:
                return seed, n
        row1 = None
        for n in row0:
            for n2 in m["routes"][n]:
                if m["nodes"][n2]["type"] == ntype:
                    return seed, n2
    raise AssertionError("cannot find target node across seeds")


def test_duplicate_reward_claim_rejected(client):
    seed, node = _find_run_with_row0_of_type(0, mapgen.REWARD)
    r = client.post("/api/runs", json={"seed": seed}).json()
    rid = r["run_id"]
    step = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node}).json()
    run = step["run"]
    assert run["reward_claimed"] is False
    assert len(run["reward_options"]) >= 1
    first = client.post(f"/api/runs/{rid}/act", json={"action": "claim_reward", "option": 0})
    assert first.status_code == 200
    assert first.json()["run"]["reward_claimed"] is True
    # 重复领奖 -> 409
    dup = client.post(f"/api/runs/{rid}/act", json={"action": "claim_reward", "option": 0})
    assert dup.status_code == 409


def test_loss_unlocks_one_new_card(client):
    seed, node = _find_run_with_row0_of_type(0, mapgen.ENCOUNTER)
    r = client.post("/api/runs", json={"seed": seed}).json()
    rid = r["run_id"]
    before = len(r["unlocked_cards"]["unlocked"])
    step = client.post(f"/api/runs/{rid}/act", json={"action": "choose_node", "node": node}).json()
    assert step["run"]["in_battle"] is True
    # 把玩家血量压到 1，敌人（哥布林，6 伤）下一击必杀
    rec = service.load_run(rid)
    rec["state"]["battle"]["entities"]["player"]["hp"] = 1
    db.save_run(rid, rec["state"]["status"], rec["state"]["position"], rec["state"])
    # 结束回合 -> 敌人行动 -> 死亡 -> 判负 -> 解锁
    r2 = client.post(f"/api/runs/{rid}/act", json={"action": "end_turn"})
    assert r2.status_code == 200
    run = r2.json()["run"]
    assert run["status"] == "lost"
    prof = client.get(f"/api/runs/{rid}").json()["unlocked_cards"]
    assert len(prof["unlocked"]) == before + 1