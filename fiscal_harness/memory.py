"""任务记忆（每个用户在每家企业最多一个未完成任务）、待办账本与长期偏好。

范围键始终是 (user_id, company_id)；跨企业偏好只有用户明确要求时才建立（company_id 为 NULL）。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 任务 ----------

def open_task(conn: sqlite3.Connection, user_id: str, company_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE user_id=? AND company_id=? AND status='open'",
                        (user_id, company_id)).fetchone()


def ensure_task(conn: sqlite3.Connection, user_id: str, company_id: str) -> sqlite3.Row:
    task = open_task(conn, user_id, company_id)
    if task:
        return task
    now = _now()
    conn.execute("INSERT INTO tasks (task_id, user_id, company_id, status, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?)", ("T-" + uuid.uuid4().hex[:10], user_id, company_id, "open", now, now))
    return open_task(conn, user_id, company_id)


def update_task(conn: sqlite3.Connection, task_id: str, *, goal: str | None = None, month: str | None = None,
                note: str | None = None) -> None:
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    notes = json.loads(task["notes"])
    if note:
        notes = (notes + [note])[-8:]  # 只保留最近的确认事实，控制上下文长度
    conn.execute("UPDATE tasks SET goal=COALESCE(?, goal), month=COALESCE(?, month), notes=?, updated_at=? "
                 "WHERE task_id=?", (goal, month, json.dumps(notes, ensure_ascii=False), _now(), task_id))


def close_task(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("UPDATE tasks SET status='done', updated_at=? WHERE task_id=?", (_now(), task_id))


def items(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM open_items WHERE task_id=? ORDER BY state, invoice_id", (task_id,)).fetchall()


def sync_purpose_items(conn: sqlite3.Connection, task_id: str, company_id: str, missing: list[str],
                       filled_now: list[str], rule_ref: str) -> None:
    """用最新一次用途检查结果更新待办：缺失的记为待处理；此前待处理、现已填写的记为完成；不再要求的记为撤销。"""
    now = _now()
    existing = {r["invoice_id"]: r for r in conn.execute(
        "SELECT * FROM open_items WHERE task_id=? AND kind='missing_purpose'", (task_id,))}
    for inv in missing:
        conn.execute("""INSERT INTO open_items (task_id, company_id, invoice_id, kind, state, rule_ref, updated_at)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(task_id, invoice_id, kind) DO UPDATE SET state='pending',
                            rule_ref=excluded.rule_ref, updated_at=excluded.updated_at""",
                     (task_id, company_id, inv, "missing_purpose", "pending", rule_ref, now))
    for inv, row in existing.items():
        if inv in missing or row["state"] != "pending":
            continue
        state = "done" if inv in filled_now else "dropped"
        conn.execute("UPDATE open_items SET state=?, updated_at=? WHERE task_id=? AND invoice_id=? AND kind=?",
                     (state, now, task_id, inv, "missing_purpose"))


def mark_item_done(conn: sqlite3.Connection, task_id: str, invoice_id: str, detail: str) -> bool:
    cur = conn.execute("UPDATE open_items SET state='done', detail=?, updated_at=? "
                       "WHERE task_id=? AND invoice_id=? AND kind='missing_purpose' AND state='pending'",
                       (detail, _now(), task_id, invoice_id))
    return cur.rowcount > 0


# ---------- 长期偏好 ----------

def preferences(conn: sqlite3.Connection, user_id: str, company_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM memories WHERE user_id=? AND status='active' "
                        "AND (company_id=? OR company_id IS NULL) ORDER BY created_at",
                        (user_id, company_id)).fetchall()


def add_preference(conn: sqlite3.Connection, user_id: str, company_id: str | None, content: str) -> str:
    mid = "M-" + uuid.uuid4().hex[:8]
    now = _now()
    conn.execute("INSERT INTO memories (memory_id, user_id, company_id, content, status, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (mid, user_id, company_id, content, "active", now, now))
    return mid


def _owned(conn: sqlite3.Connection, memory_id: str, user_id: str, company_id: str) -> sqlite3.Row | None:
    # 只能操作本用户、本企业（或本用户跨企业）的记忆
    return conn.execute("SELECT * FROM memories WHERE memory_id=? AND user_id=? AND status='active' "
                        "AND (company_id=? OR company_id IS NULL)", (memory_id, user_id, company_id)).fetchone()


def update_preference(conn: sqlite3.Connection, memory_id: str, user_id: str, company_id: str,
                      content: str) -> bool:
    if not _owned(conn, memory_id, user_id, company_id):
        return False
    conn.execute("UPDATE memories SET content=?, revision=revision+1, updated_at=? WHERE memory_id=?",
                 (content, _now(), memory_id))
    return True


def delete_preference(conn: sqlite3.Connection, memory_id: str, user_id: str, company_id: str) -> bool:
    """删除时清空正文，只留标识与时间，确保旧内容不会再进入模型输入。"""
    if not _owned(conn, memory_id, user_id, company_id):
        return False
    conn.execute("UPDATE memories SET content=NULL, status='deleted', revision=revision+1, updated_at=? "
                 "WHERE memory_id=?", (_now(), memory_id))
    return True
