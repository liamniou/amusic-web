import json
import sqlite3
import threading
import time

from settings import DATA_DIR

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    is_admin INTEGER NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    subsonic_token TEXT NOT NULL,
    subsonic_salt TEXT NOT NULL,
    is_admin INTEGER NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    username TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT,
    title TEXT,
    artist TEXT,
    artwork TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT,
    message TEXT,
    counts TEXT NOT NULL DEFAULT '{}',
    nd_albums TEXT NOT NULL DEFAULT '[]',
    nd_playlist TEXT,
    log TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tracks (
    job_id INTEGER NOT NULL,
    id TEXT NOT NULL,
    pos INTEGER NOT NULL,
    title TEXT,
    artist TEXT,
    album TEXT,
    track INTEGER,
    disc INTEGER,
    isrc TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    message TEXT,
    path TEXT,
    format TEXT,
    nd_song_id TEXT,
    nd_album_id TEXT,
    PRIMARY KEY (job_id, id)
);
"""

JSON_COLS = {"options", "counts", "nd_albums", "nd_playlist"}


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DATA_DIR / "amusic-web.db", check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


def q(sql: str, *args) -> list[dict]:
    with _lock:
        return [_row(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, *args) -> dict | None:
    with _lock:
        return _row(conn().execute(sql, args).fetchone())


def ex(sql: str, *args) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def update(table: str, where: dict, **values):
    if not values:
        return
    cols = ", ".join(f"{k} = ?" for k in values)
    conds = " AND ".join(f"{k} = ?" for k in where)
    args = [json.dumps(v) if k in JSON_COLS and v is not None else v for k, v in values.items()]
    ex(f"UPDATE {table} SET {cols} WHERE {conds}", *args, *where.values())


def append_log(job_id: int, text: str):
    ex("UPDATE jobs SET log = substr(log || ?, -200000) WHERE id = ?", text, job_id)


def purge_sessions():
    ex("DELETE FROM sessions WHERE expires < ?", time.time())
