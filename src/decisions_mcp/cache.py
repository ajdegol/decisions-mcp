"""Content-addressed result cache. Never stores inputs or credentials."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def request_key(body: dict) -> str:
    encoded = json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ResultCache:
    def __init__(self, path: Path | str, ttl: int = 86400):
        self.path, self.ttl = str(path), ttl

    @contextmanager
    def _connect(self):
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                key TEXT PRIMARY KEY, response_json TEXT NOT NULL, created_at REAL NOT NULL
            )
        """)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT response_json, created_at FROM results WHERE key=?", (key,)
            ).fetchone()
        if row and row[1] + self.ttl > time.time():
            return json.loads(row[0]), row[1]
        return None

    def put(self, key: str, response: dict):
        created = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO results VALUES (?, ?, ?)",
                (key, json.dumps(response, ensure_ascii=False), created),
            )
            db.execute("DELETE FROM results WHERE created_at<?", (created - self.ttl,))
        return created
