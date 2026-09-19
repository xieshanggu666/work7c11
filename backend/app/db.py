"""SQLite 初始化：managed 文件位于 backend/data/game.db。"""
import json
import os
import sqlite3
from threading import Lock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("GAME_DB_PATH", os.path.join(DATA_DIR, "game.db"))

_lock = Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    position TEXT NOT NULL,          -- 当前地图节点 id
    map_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
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
"""


def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        conn = get_conn()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


def insert_run(run_id, seed, status, position, map_data, state):
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO runs(id,seed,status,position,map_json,state_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (run_id, seed, status, position,
                 json.dumps(map_data, ensure_ascii=False),
                 json.dumps(state, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()


def load_run(run_id):
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return {
        "id": row["id"], "seed": row["seed"], "status": row["status"],
        "position": row["position"], "map": json.loads(row["map_json"]),
        "state": json.loads(row["state_json"]),
    }


def save_run(run_id, status, position, state):
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE runs SET status=?, position=?, state_json=?, updated_at=datetime('now') WHERE id=?",
                (status, position, json.dumps(state, ensure_ascii=False), run_id),
            )
            conn.commit()
        finally:
            conn.close()


def update_run_status(run_id, status):
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE runs SET status=?, updated_at=datetime('now') WHERE id=?",
                (status, run_id),
            )
            conn.commit()
        finally:
            conn.close()


def next_seq(run_id):
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM battle_events WHERE run_id=?", (run_id,)
            ).fetchone()
            return row["m"] + 1
        finally:
            conn.close()


def append_event(run_id, seq, action, payload):
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO battle_events(run_id,seq,action,payload_json) VALUES(?,?,?,?)",
                (run_id, seq, action, json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()


def load_events(run_id):
    with _lock:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT seq, action, payload_json FROM battle_events WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
    return [{"seq": r["seq"], "action": r["action"], "payload": json.loads(r["payload_json"])} for r in rows]


def get_profile():
    with _lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT unlocked_cards FROM profile WHERE id='single'").fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return json.loads(row["unlocked_cards"])


def upsert_profile(unlocked_cards):
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO profile(id,unlocked_cards) VALUES('single',?) "
                "ON CONFLICT(id) DO UPDATE SET unlocked_cards=excluded.unlocked_cards",
                (json.dumps(unlocked_cards, ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()