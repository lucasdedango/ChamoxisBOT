"""SQLite task and outbox persistence; atomic claims and stable request IDs."""
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
          payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
          updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.commit()

    def create_task(self, request_id, payload):
        wire = json.dumps(payload, sort_keys=True)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, 'queued', NULL, ?)",
                            (str(uuid4()), request_id, wire, time.time()))
        row = self.db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        if row["payload"] != wire:
            raise Conflict("Request ID already used with different parameters")
        return self._task(row)

    def _task(self, row):
        if row is None:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out

    def task(self, task_id):
        return self._task(self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def tasks(self, states=None):
        rows = self.db.execute("SELECT * FROM tasks ORDER BY updated DESC").fetchall()
        return [self._task(r) for r in rows if states is None or r["state"] in states]

    def transition(self, task_id, state, result=None):
        with self.db:
            if result is None:
                self.db.execute("UPDATE tasks SET state=?, updated=? WHERE id=?", (state, time.time(), task_id))
            else:
                self.db.execute("UPDATE tasks SET state=?, result=?, updated=? WHERE id=?",
                                (state, json.dumps(result), time.time(), task_id))

    def claim(self, task_id, expected, new):
        with self.db:
            return self.db.execute("UPDATE tasks SET state=?, updated=? WHERE id=? AND state=?",
                                   (new, time.time(), task_id, expected)).rowcount == 1

    def event(self, event):
        wire = event.model_dump_json() if hasattr(event, "model_dump_json") else json.dumps(event)
        payload = json.loads(wire)
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO events(id,payload) VALUES (?,?)",
                                       (payload["id"], wire)).rowcount
        return bool(inserted)

    def pending_events(self):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM events WHERE delivered=0 AND next_attempt<=? ORDER BY rowid LIMIT 100", (time.time(),))]

    def delivered(self, event_id):
        with self.db:
            self.db.execute("UPDATE events SET delivered=1 WHERE id=?", (event_id,))

    def retry(self, event_id):
        row = self.db.execute("SELECT attempts FROM events WHERE id=?", (event_id,)).fetchone()
        attempts = row[0] + 1
        with self.db:
            self.db.execute("UPDATE events SET attempts=?, next_attempt=? WHERE id=?",
                            (attempts, time.time() + min(300, 2 ** min(attempts, 8)), event_id))

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
