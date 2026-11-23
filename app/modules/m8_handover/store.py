import json
import sqlite3
from contextlib import closing

from app.config import database_path
from app.manifest import canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS handover_inbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_batches (
    batch_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS handover_notices (
    letter_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    letter_no TEXT NOT NULL,
    kind TEXT NOT NULL,
    effect TEXT NOT NULL,
    chain TEXT NOT NULL,
    year INTEGER NOT NULL,
    volume INTEGER NOT NULL,
    issue_first INTEGER NOT NULL,
    issue_last INTEGER NOT NULL,
    document TEXT NOT NULL,
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_quarantine (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_seq INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    line INTEGER NOT NULL,
    code TEXT NOT NULL,
    fields TEXT NOT NULL,
    raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_replays (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_seq INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    line INTEGER NOT NULL,
    original_seq INTEGER NOT NULL,
    letter_no TEXT NOT NULL,
    digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_snapshots (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, source TEXT NOT NULL, document TEXT NOT NULL)")
        db.executescript(SCHEMA)
    except BaseException:
        db.close()
        raise
    return db


def register_batch(db, batch_id, received_at, note):
    db.execute("INSERT INTO handover_batches(batch_id, received_at, note) VALUES (?, ?, ?)",
               (batch_id, received_at, note))


def stamp_inbox(db, batch_id, received_at, raw):
    """原件先入台账并独立提交，任何后续失败都不删除原件。"""
    cursor = db.execute("INSERT INTO handover_inbox(batch_id, received_at, raw) VALUES (?, ?, ?)",
                        (batch_id, received_at, canonical(raw)))
    return cursor.lastrowid


def pinned_rules(db):
    row = db.execute("SELECT value FROM handover_meta WHERE key = 'rules'").fetchone()
    return None if row is None else json.loads(row[0])


def pin_rules(db, rules, digest):
    db.execute("INSERT INTO handover_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
               ("rules", canonical({"rules": rules, "digest": digest})))


def find_notice(db, digest):
    row = db.execute("SELECT letter_id, seq, letter_no FROM handover_notices WHERE digest = ?", (digest,)).fetchone()
    return None if row is None else {"letter_id": row[0], "seq": row[1], "letter_no": row[2]}


def insert_notice(db, letter_id, seq, batch_id, notice, digest):
    db.execute(
        "INSERT INTO handover_notices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (letter_id, seq, batch_id, notice["letter_no"], notice["kind"], notice["effect"], notice["chain"],
         notice["year"], notice["cover"]["volume"], notice["cover"]["first"], notice["cover"]["last"],
         canonical(notice), digest))


def insert_quarantine(db, inbox_seq, batch_id, line, code, fields, raw):
    db.execute(
        "INSERT INTO handover_quarantine(inbox_seq, batch_id, line, code, fields, raw) VALUES (?, ?, ?, ?, ?, ?)",
        (inbox_seq, batch_id, line, code, json.dumps(fields, ensure_ascii=False), canonical(raw)))


def insert_replay(db, inbox_seq, batch_id, line, original_seq, letter_no, digest):
    db.execute(
        "INSERT INTO handover_replays(inbox_seq, batch_id, line, original_seq, letter_no, digest) VALUES (?, ?, ?, ?, ?, ?)",
        (inbox_seq, batch_id, line, original_seq, letter_no, digest))


def load_notices(db):
    rows = db.execute("SELECT document FROM handover_notices ORDER BY seq").fetchall()
    return [json.loads(row[0]) for row in rows]


def load_quarantine(db):
    rows = db.execute("SELECT inbox_seq, batch_id, line, code, fields, raw FROM handover_quarantine ORDER BY seq").fetchall()
    return [{"inbox_seq": r[0], "batch_id": r[1], "line": r[2], "code": r[3],
             "fields": json.loads(r[4]), "raw": json.loads(r[5])} for r in rows]


def load_replays(db):
    rows = db.execute(
        "SELECT inbox_seq, batch_id, line, original_seq, letter_no, digest FROM handover_replays ORDER BY seq").fetchall()
    return [{"inbox_seq": r[0], "batch_id": r[1], "line": r[2], "original_seq": r[3],
             "letter_no": r[4], "digest": r[5]} for r in rows]


def load_inbox(db):
    rows = db.execute("SELECT seq, batch_id, received_at, raw FROM handover_inbox ORDER BY seq").fetchall()
    return [{"seq": r[0], "batch_id": r[1], "received_at": r[2], "raw": json.loads(r[3])} for r in rows]


def load_batches(db):
    rows = db.execute("SELECT batch_id, received_at, note FROM handover_batches ORDER BY rowid").fetchall()
    return [{"batch_id": r[0], "received_at": r[1], "note": r[2]} for r in rows]


def save_snapshot(db, created_at, document):
    """同摘要不产生新版本，返回 (版本号, 是否复用旧版, 文档)。"""
    digest = document["digest"]
    existing = db.execute("SELECT version, document FROM handover_snapshots WHERE digest = ?", (digest,)).fetchone()
    if existing is not None:
        return existing[0], True, json.loads(existing[1])
    cursor = db.execute("INSERT INTO handover_snapshots(digest, created_at, document) VALUES (?, ?, ?)",
                        (digest, created_at, canonical(document)))
    return cursor.lastrowid, False, document


def current_snapshot(db):
    row = db.execute("SELECT document FROM handover_snapshots ORDER BY version DESC LIMIT 1").fetchone()
    return None if row is None else json.loads(row[0])


def get_snapshot(db, version):
    row = db.execute("SELECT document FROM handover_snapshots WHERE version = ?", (version,)).fetchone()
    return None if row is None else json.loads(row[0])


def list_snapshots(db):
    rows = db.execute("SELECT version, created_at, digest FROM handover_snapshots ORDER BY version").fetchall()
    return [{"version": r[0], "created_at": r[1], "digest": r[2]} for r in rows]


def read_state():
    with closing(connect()) as db:
        return {
            "rules": pinned_rules(db),
            "notices": load_notices(db),
            "quarantine": load_quarantine(db),
            "replays": load_replays(db),
            "inbox": load_inbox(db),
            "batches": load_batches(db),
            "snapshots": list_snapshots(db),
            "current": current_snapshot(db),
        }
