"""SQLite 初始化与访问：managed 文件位于 backend/data/game.db。

并发一致性设计：
- 所有写操作经单个连接在一个事务里提交（BEGIN IMMEDIATE），行动的
  「存档 + 动作日志 + 解锁奖励」要么全部生效、要么全部回滚，杜绝写入失败
  造成的存档/回放分叉；
- WAL 日志 + busy_timeout，读写并发不再因锁竞争互相报错；
- per-run 进程内互斥锁串行化同一局的读改写；幂等表 act_requests 记录
  客户端请求令牌，重复请求（双击/超时重试）返回首次结果而非再执行一次；
- 旧库启动时自动补齐后增列与幂等表（向后兼容已有 game.db）。
"""
import json
import os
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("GAME_DB_PATH", os.path.join(DATA_DIR, "game.db"))

_lock = threading.RLock()
_conn = None  # 进程内单连接（check_same_thread=False + 写锁串行化）

# 每个 run 一把互斥锁：同一局的行动/迁移在进程内串行，不同局互不阻塞
_run_locks = {}
_run_locks_guard = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    position TEXT NOT NULL,          -- 当前地图节点 id
    map_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    rev INTEGER NOT NULL DEFAULT 1,  -- 乐观版本号：每次原子提交 +1
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

-- 行动请求幂等：同一 run + request_id 只提交一次，重复请求返回首次响应
CREATE TABLE IF NOT EXISTS act_requests (
    run_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, request_id)
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL：读者不阻塞写者；busy_timeout 让锁竞争等待而不是立即报错
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn():
    """供测试/运维使用的独立连接（只读探查或测试夹具清表）。

    业务写入不要自行开连接提交——那会绕过单连接事务边界。需要事务时用
    ``transaction()``；需要在行动事务里完成多表写入时用 ``act_transaction()``。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    global _conn
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            # 旧库兼容：补齐后增列（ALTER 失败说明列已存在，忽略）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "rev" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN rev INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        finally:
            conn.close()
        _conn = _connect()


def run_lock(run_id):
    """返回该局专属的进程内互斥锁（串行化同一 run 的读-改-写）。"""
    with _run_locks_guard:
        lk = _run_locks.get(run_id)
        if lk is None:
            lk = threading.RLock()
            _run_locks[run_id] = lk
        return lk


# ---------- 低层事务 ----------
def transaction():
    """返回一个立即写事务上下文：块内所有语句一起提交，异常整体回滚。"""
    return _Tx(_conn)


class _Tx:
    """对共享单连接加全局锁后开启立即写事务：读写互不交错。

    进程内所有 SQL 都经同一连接且被 _lock 串行；per-run 锁在更外层保证
    “读-改-写”的逻辑原子性。
    """

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        _lock.acquire()
        if self._conn is None:
            init_db()
            self._conn = _conn
        # IMMEDIATE：进入即拿写锁，消除两个写事务交错提交（丢失更新）的窗口
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            _lock.release()
        return False


# ---------- runs ----------
def insert_run(conn, run_id, seed, status, position, map_data, state):
    conn.execute(
        "INSERT INTO runs(id,seed,status,position,map_json,state_json,rev,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,1,datetime('now'),datetime('now'))",
        (run_id, seed, status, position,
         json.dumps(map_data, ensure_ascii=False),
         json.dumps(state, ensure_ascii=False)),
    )


def _row_to_run(row):
    return {
        "id": row["id"], "seed": row["seed"], "status": row["status"],
        "position": row["position"], "map": json.loads(row["map_json"]),
        "state": json.loads(row["state_json"]), "rev": row["rev"],
    }


def load_run(run_id):
    """读取单局（含乐观版本号 rev）。使用共享连接，读不到未提交数据。"""
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return None if row is None else _row_to_run(row)


def save_run_run(conn, run_id, status, position, state, expected_rev=None):
    """在事务内更新存档并推进 rev。

    expected_rev 非 None 时做乐观并发检查：存档已被其它请求推进则抛出
    ConcurrentModification，由上层转 409（状态冲突）。
    """
    if expected_rev is None:
        conn.execute(
            "UPDATE runs SET status=?, position=?, state_json=?, rev=rev+1, "
            "updated_at=datetime('now') WHERE id=?",
            (status, position, json.dumps(state, ensure_ascii=False), run_id),
        )
        return
    cur = conn.execute(
        "UPDATE runs SET status=?, position=?, state_json=?, rev=rev+1, "
        "updated_at=datetime('now') WHERE id=? AND rev=?",
        (status, position, json.dumps(state, ensure_ascii=False), run_id, expected_rev),
    )
    if cur.rowcount == 0:
        raise ConcurrentModification(f"run {run_id} changed concurrently (rev {expected_rev})")


# ---------- battle_events ----------
def next_seq_conn(conn, run_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM battle_events WHERE run_id=?", (run_id,)
    ).fetchone()
    return row["m"] + 1


def append_event_conn(conn, run_id, seq, action, payload):
    conn.execute(
        "INSERT INTO battle_events(run_id,seq,action,payload_json) VALUES(?,?,?,?)",
        (run_id, seq, action, json.dumps(payload, ensure_ascii=False)),
    )


def load_events(run_id):
    """读取动作日志（只读）。

    异常日志兼容：payload_json 损坏（NULL/截断/非法 JSON）的行不抹掉整段回放，
    返回空 payload 并由回放推演标注为 error（前端仍可跳转其余步骤）。
    """
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        rows = conn.execute(
            "SELECT seq, action, payload_json FROM battle_events WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
    out = []
    for r in rows:
        raw = r["payload_json"]
        try:
            payload = json.loads(raw) if raw is not None else {}
        except (ValueError, TypeError):
            payload = {"_corrupt": True, "_raw": raw[:200] if isinstance(raw, str) else None}
        if not isinstance(payload, dict):
            payload = {"_corrupt": True}
        out.append({"seq": r["seq"], "action": r["action"], "payload": payload})
    return out


# ---------- 行动请求幂等 ----------
def get_idempotent(conn, run_id, request_id):
    """查重复请求的首次响应；request_id 为空或查不到返回 None。"""
    if not request_id:
        return None
    row = conn.execute(
        "SELECT seq, response_json FROM act_requests WHERE run_id=? AND request_id=?",
        (run_id, request_id),
    ).fetchone()
    if row is None:
        return None
    return {"seq": row["seq"], "response": json.loads(row["response_json"])}


def put_idempotent(conn, run_id, request_id, seq, response):
    if not request_id:
        return
    conn.execute(
        "INSERT INTO act_requests(run_id,request_id,seq,response_json,created_at) "
        "VALUES(?,?,?,?,datetime('now'))",
        (run_id, request_id, seq, json.dumps(response, ensure_ascii=False)),
    )


# ---------- profile ----------
def get_profile_conn(conn):
    row = conn.execute("SELECT unlocked_cards FROM profile WHERE id='single'").fetchone()
    return None if row is None else json.loads(row["unlocked_cards"])


def get_profile():
    with _lock:
        conn = _conn
        if conn is None:
            init_db()
            conn = _conn
        row = conn.execute("SELECT unlocked_cards FROM profile WHERE id='single'").fetchone()
    return None if row is None else json.loads(row["unlocked_cards"])


def upsert_profile_conn(conn, unlocked_cards):
    conn.execute(
        "INSERT INTO profile(id,unlocked_cards) VALUES('single',?) "
        "ON CONFLICT(id) DO UPDATE SET unlocked_cards=excluded.unlocked_cards",
        (json.dumps(unlocked_cards, ensure_ascii=False),),
    )


# ---------- 独立包装：迁移/测试/运维用（单表原子即可的场景） ----------
def save_run(run_id, status, position, state):
    with transaction() as conn:
        save_run_run(conn, run_id, status, position, state)


def next_seq(run_id):
    with transaction() as conn:
        return next_seq_conn(conn, run_id)


def append_event(run_id, seq, action, payload):
    with transaction() as conn:
        append_event_conn(conn, run_id, seq, action, payload)


def upsert_profile(unlocked_cards):
    with transaction() as conn:
        upsert_profile_conn(conn, unlocked_cards)


class ConcurrentModification(Exception):
    """乐观版本号不匹配：存档在本请求处理期间被其它提交推进。"""
    pass
