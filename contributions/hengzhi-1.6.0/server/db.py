"""SQLite 存储层：任务、消息、知识库分片。"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    provider    TEXT,
    model       TEXT,
    created_at  REAL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT,
    reasoning   TEXT,
    tool_calls  TEXT,
    attachments TEXT,
    seq         INTEGER,
    created_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_msg_task ON messages(task_id, seq);
CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kb       TEXT,
    file     TEXT,
    chunk_no INTEGER,
    text     TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunk_kb ON chunks(kb);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        yield c
        c.commit()
    finally:
        c.close()


def init():
    try:
        with conn() as c:
            c.executescript(SCHEMA)
    except sqlite3.DatabaseError as e:
        # 数据库损坏（例如上次异常退出留下残缺文件）：备份后重建，保证应用可用
        broken = config.DB_PATH.with_name(
            f"agent.db.broken-{int(time.time())}")
        try:
            config.DB_PATH.rename(broken)
            for suffix in ("-wal", "-shm", "-journal"):
                p = config.DB_PATH.with_name("agent.db" + suffix)
                if p.exists():
                    p.rename(p.with_name(p.name + f".broken-{int(time.time())}"))
        except Exception:
            raise RuntimeError(
                f"数据库初始化失败：{e}。请检查 data/ 目录写入权限，"
                f"或手动删除 {config.DB_PATH} 后重试。") from e
        with conn() as c:
            c.executescript(SCHEMA)
        print(f"[提示] 原数据库无法打开，已备份为 {broken.name} 并新建数据库。")


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- 任务

def create_task(title: str = "新任务", provider: str = "", model: str = "") -> dict:
    tid = new_id()
    ts = now()
    with conn() as c:
        c.execute(
            "INSERT INTO tasks(id,title,provider,model,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (tid, title, provider, model, ts, ts),
        )
    return {"id": tid, "title": title, "provider": provider, "model": model,
            "created_at": ts, "updated_at": ts, "message_count": 0}


def list_tasks() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.task_id=t.id) AS message_count
               FROM tasks t ORDER BY t.updated_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_task(tid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def touch_task(tid: str, title: str | None = None,
               provider: str | None = None, model: str | None = None):
    sets, vals = ["updated_at=?"], [now()]
    if title is not None:
        sets.append("title=?")
        vals.append(title)
    if provider is not None:
        sets.append("provider=?")
        vals.append(provider)
    if model is not None:
        sets.append("model=?")
        vals.append(model)
    vals.append(tid)
    with conn() as c:
        c.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", vals)


def delete_task(tid: str):
    with conn() as c:
        c.execute("DELETE FROM messages WHERE task_id=?", (tid,))
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))


# ---------------------------------------------------------------- 消息

def add_message(task_id: str, role: str, content: str = "", reasoning: str = "",
                tool_calls: Any = None, attachments: Any = None) -> dict:
    mid = new_id()
    ts = now()
    with conn() as c:
        row = c.execute("SELECT COALESCE(MAX(seq),0)+1 AS s FROM messages WHERE task_id=?",
                        (task_id,)).fetchone()
        seq = row["s"]
        c.execute(
            """INSERT INTO messages(id,task_id,role,content,reasoning,tool_calls,attachments,seq,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (mid, task_id, role, content, reasoning,
             json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
             json.dumps(attachments, ensure_ascii=False) if attachments else None,
             seq, ts),
        )
    return {"id": mid, "task_id": task_id, "role": role, "content": content,
            "reasoning": reasoning, "tool_calls": tool_calls,
            "attachments": attachments, "seq": seq, "created_at": ts}


def list_messages(task_id: str, limit: int | None = None) -> list[dict]:
    with conn() as c:
        if limit:
            rows = c.execute(
                "SELECT * FROM messages WHERE task_id=? ORDER BY seq DESC LIMIT ?",
                (task_id, limit)).fetchall()
            rows = list(reversed(rows))
        else:
            rows = c.execute(
                "SELECT * FROM messages WHERE task_id=? ORDER BY seq ASC", (task_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tool_calls"] = json.loads(d["tool_calls"]) if d["tool_calls"] else None
        d["attachments"] = json.loads(d["attachments"]) if d["attachments"] else None
        out.append(d)
    return out


def clear_messages(task_id: str):
    with conn() as c:
        c.execute("DELETE FROM messages WHERE task_id=?", (task_id,))


# ---------------------------------------------------------------- 知识库分片

def replace_kb_chunks(kb: str, items: Iterable[tuple[str, int, str]]):
    with conn() as c:
        c.execute("DELETE FROM chunks WHERE kb=?", (kb,))
        c.executemany(
            "INSERT INTO chunks(kb,file,chunk_no,text) VALUES(?,?,?,?)",
            [(kb, f, n, t) for (f, n, t) in items],
        )


def delete_kb_chunks(kb: str):
    with conn() as c:
        c.execute("DELETE FROM chunks WHERE kb=?", (kb,))


def all_chunks(kbs: list[str] | None = None) -> list[dict]:
    with conn() as c:
        if kbs:
            q = ",".join("?" * len(kbs))
            rows = c.execute(f"SELECT * FROM chunks WHERE kb IN ({q})", kbs).fetchall()
        else:
            rows = c.execute("SELECT * FROM chunks").fetchall()
    return [dict(r) for r in rows]


def kb_stats() -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT kb, COUNT(*) AS n, COUNT(DISTINCT file) AS files FROM chunks GROUP BY kb"
        ).fetchall()
    return {r["kb"]: {"chunks": r["n"], "files": r["files"]} for r in rows}
