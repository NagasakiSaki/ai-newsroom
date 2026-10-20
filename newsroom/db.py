import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                    collection TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(collection, id));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                    subject TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, collection, record):
        with self.connection() as db:
            db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(collection,id) DO UPDATE SET data=excluded.data",
                       (collection, record["id"], json.dumps(record, ensure_ascii=False)))
        return record

    def get(self, collection, record_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM records WHERE collection=? AND id=?", (collection, record_id)).fetchone()
        if not row:
            raise KeyError(record_id)
        return json.loads(row["data"])

    def delete(self, collection, record_id):
        with self.connection() as db:
            return db.execute("DELETE FROM records WHERE collection=? AND id=?", (collection, record_id)).rowcount

    def all(self, collection):
        with self.connection() as db:
            rows = db.execute("SELECT data FROM records WHERE collection=? ORDER BY rowid DESC", (collection,)).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def event(self, subject, stage, message):
        with self.connection() as db:
            db.execute("INSERT INTO events(at,subject,stage,message) VALUES (?,?,?,?)", (now(), subject, stage, message))

    def events(self, limit=100):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))]
