import os
import tempfile

_tmp = os.path.join(tempfile.gettempdir(), "game_test.db")
if os.path.exists(_tmp):
    os.remove(_tmp)
os.environ["GAME_DB_PATH"] = _tmp

import pytest
from fastapi.testclient import TestClient

from app import db, service
from app.main import app


@pytest.fixture(autouse=True)
def reset_db():
    db.init_db()
    # 清空 profile/表以便隔离
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM runs")
        conn.execute("DELETE FROM battle_events")
        conn.execute("DELETE FROM profile")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM runs")
        conn.execute("DELETE FROM battle_events")
        conn.execute("DELETE FROM profile")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c