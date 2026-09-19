"""SQLite 初始化与访问：managed 文件位于 backend/data/game.db。

写路径统一走 :class:`Transaction`（BEGIN IMMEDIATE 单连接事务）：
行动的「存档落库 + 动作日志追加 + 解锁奖励 + 幂等记录」必须在同一个事务里提交，
任何一步失败整体回滚，从根上消除“存档已变、日志未写/奖励半发”的不一致。
runs.revision 是乐观并发版本号：act 读档时记下，提交时 CAS 更新，
被并发/跨进程抢先写入时抛 StateConflict（HTTP 409），不覆盖他人结果。
"""
import contextlib
import json
import os
import sqlite3
from threading import Lock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("GAME_DB_PATH", os.path.join(DATA_DIR, "game.db"))

# 仅用于 DDL 等极短临界区；写事务的真正互斥由 SQLite BEGIN IMMEDIATE 保证（跨进程有效）。
_lock = Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    position TEXT NOT NULL,          -- 当前地图节点 id
    map_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,  -- 乐观并发版本：每次提交 +1
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS battle_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS profile (
    id TEXT PRIMARY KEY,             -- 'single'
    unlocked_cards TEXT NOT NULL
);

-- 行动幂等：同一 (run_id, req_id) 的重试/重复提交直接返回首次结果，绝不重复生效
CREATE TABLE IF NOT EXISTS idempotent_requests (
    run_id TEXT NOT NULL,
    req_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, req_id)
);
"""


class Conflict(Exception):
    """提交时发现状态已被其他事务改写（CAS 失败 / 序号冲突）。"""


class Transaction:
    """单个写事务：BEGIN IMMEDIATE 立即拿写锁，期间的读写都在同一连接/快照上，
    正常退出 commit，异常 rollback。跨进程由 SQLite 文件锁串行化。"""

    def __init__(self):
        self.conn = get_conn()

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False

    # ---------- runs ----------
    def insert_run(self, run_id, seed, status, position, map_data, state):
        self.conn.execute(
            "INSERT INTO runs(id,seed,status,position,map_json,state_json,revision,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,1,datetime('now'),datetime('now'))",
            (run_id, seed, status, position,
             json.dumps(map_data, ensure_ascii=False),
             json.dumps(state, ensure_ascii=False)),
        )

    def load_run(self, run_id):
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _row_to_run(row)

    def save_run(self, run_id, status, position, state):
        """无条件更新（续局迁移等无并发上下文场景）。"""
        self.conn.execute(
            "UPDATE runs SET status=?, position=?, state_json=?, revision=revision+1, "
            "updated_at=datetime('now') WHERE id=?",
            (status, position, json.dumps(state, ensure_ascii=False), run_id),
        )

    def save_run_cas(self, run_id, status, position, state, expected_revision):
        """乐观锁更新：仅当 revision 未变时写入并 +1；被抢先提交则抛 Conflict。"""
        cur = self.conn.execute(
            "UPDATE runs SET status=?, position=?, state_json=?, revision=revision+1, "
            "updated_at=datetime('now') WHERE id=? AND revision=?",
            (status, position, json.dumps(state, ensure_ascii=False), run_id, expected_revision),
        )
        if cur.rowcount == 0:
            raise Conflict(f"run {run_id} changed concurrently (expected revision {expected_revision})")

    # ---------- battle_events ----------
    def max_seq(self, run_id):
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0) AS m FROM battle_events WHERE run_id=?", (run_id,)
        ).fetchone()
        return row["m"]

    def next_seq(self, run_id):
        return self.max_seq(run_id) + 1

    def insert_event(self, run_id, seq, action, payload):
        try:
            self.conn.execute(
                "INSERT INTO battle_events(run_id,seq,action,payload_json) VALUES(?,?,?,?)",
                (run_id, seq, action, json.dumps(payload, ensure_ascii=False)),
            )
        except sqlite3.IntegrityError as e:
            # 同一序号已被并发事务占用：日志必须连续，交给上层按冲突处理（整体回滚）
            raise Conflict(str(e)) from e

    # ---------- profile ----------
    def get_profile(self):
        row = self.conn.execute(
            "SELECT unlocked_cards FROM profile WHERE id='single'"
        ).fetchone()
        return json.loads(row["unlocked_cards"]) if row else None

    def upsert_profile(self, unlocked_cards):
        self.conn.execute(
            "INSERT INTO profile(id,unlocked_cards) VALUES('single',?) "
            "ON CONFLICT(id) DO UPDATE SET unlocked_cards=excluded.unlocked_cards",
            (json.dumps(unlocked_cards, ensure_ascii=False),),
        )

    # ---------- 幂等 ----------
    def get_idempotent(self, run_id, req_id):
        row = self.conn.execute(
            "SELECT seq, response_json FROM idempotent_requests WHERE run_id=? AND req_id=?",
            (run_id, req_id),
        ).fetchone()
        if row is None:
            return None
        return {"seq": row["seq"], "response": json.loads(row["response_json"])}

    def put_idempotent(self, run_id, req_id, seq, response):
        self.conn.execute(
            "INSERT INTO idempotent_requests(run_id,req_id,seq,response_json,created_at) "
            "VALUES(?,?,?,?,datetime('now'))",
            (run_id, req_id, seq, json.dumps(response, ensure_ascii=False)),
        )


def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with _lock:
        conn = get_conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # 旧库平滑升级：补齐 revision 列（默认 1）
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
            if "revision" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        finally:
            conn.close()


def _row_to_run(row):
    if row is None:
        return None
    keys = row.keys()
    return {
        "id": row["id"], "seed": row["seed"], "status": row["status"],
        "position": row["position"], "map": json.loads(row["map_json"]),
        "state": json.loads(row["state_json"]),
        "revision": row["revision"] if "revision" in keys else 1,
    }


# ---------- 兼容层：测试/管理路径仍可逐函数调用（各自开独立事务） ----------
@contextlib.contextmanager
def transaction():
    t = Transaction()
    with t:
        yield t


def insert_run(run_id, seed, status, position, map_data, state):
    with transaction() as t:
        t.insert_run(run_id, seed, status, position, map_data, state)


def load_run(run_id):
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()
    return _row_to_run(row)


def save_run(run_id, status, position, state):
    with transaction() as t:
        t.save_run(run_id, status, position, state)


def update_run_status(run_id, status):
    with transaction() as t:
        t.conn.execute(
            "UPDATE runs SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, run_id),
        )


def next_seq(run_id):
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM battle_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return row["m"] + 1
        finally:
            conn.close()


def append_event(run_id, seq, action, payload):
    with transaction() as t:
        t.insert_event(run_id, seq, action, payload)


def load_events(run_id):
    """读出整局动作日志。

    异常日志兼容：单条 payload 损坏（JSON 解析失败）不抹掉整段回放，
    以 {"_corrupt": True, "raw": ...} 占位返回，由回放标记为 error 帧。
    """
    with _lock:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT seq, action, payload_json FROM battle_events WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
    events = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            payload = {"_corrupt": True, "raw": r["payload_json"]}
        events.append({"seq": r["seq"], "action": r["action"], "payload": payload})
    return events


def get_profile():
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT unlocked_cards FROM profile WHERE id='single'").fetchone()
        finally:
            conn.close()
    return json.loads(row["unlocked_cards"]) if row else None


def upsert_profile(unlocked_cards):
    with transaction() as t:
        t.upsert_profile(unlocked_cards)
