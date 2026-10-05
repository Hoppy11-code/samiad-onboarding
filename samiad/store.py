"""Small SQLite store on the Railway volume: tokens, run times, audit log, shadow log."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
create table if not exists kv (key text primary key, value text, updated_at text);
create table if not exists audit (
    id integer primary key autoincrement,
    at text, deal_id text, action text, detail text, shadow integer
);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.db = sqlite3.connect(data_dir / "state.sqlite3")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # key/value ----------------------------------------------------------
    def get(self, key: str, default=None):
        row = self.db.execute("select value from kv where key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute(
            "insert into kv(key,value,updated_at) values(?,?,?) "
            "on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), _now()),
        )
        self.db.commit()

    # audit --------------------------------------------------------------
    def log(self, deal_id: str, action: str, detail: dict | str, shadow: bool) -> None:
        self.db.execute(
            "insert into audit(at,deal_id,action,detail,shadow) values(?,?,?,?,?)",
            (_now(), deal_id, action, json.dumps(detail, default=str), int(shadow)),
        )
        self.db.commit()

    def recent(self, since_iso: str) -> list[dict]:
        rows = self.db.execute(
            "select at,deal_id,action,detail,shadow from audit where at>=? order by id", (since_iso,)
        ).fetchall()
        return [dict(at=a, deal_id=d, action=x, detail=json.loads(t), shadow=bool(s))
                for a, d, x, t, s in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
