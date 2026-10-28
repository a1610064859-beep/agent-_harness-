"""SQLite 表结构。业务当前数据、规则、记忆、任务与运行记录都在同一个库里，原始 Excel 只在导入时读取。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    level TEXT NOT NULL CHECK (level IN ('none', 'read', 'write')),
    PRIMARY KEY (user_id, company_id)
);
CREATE TABLE IF NOT EXISTS invoices (
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    invoice_id TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    seller TEXT NOT NULL,
    item TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    purpose TEXT,
    remark TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT,
    PRIMARY KEY (company_id, invoice_id)
);
-- 从发票文件导入的票面全部字段与来源；invoices 只存业务上用到的几列
CREATE TABLE IF NOT EXISTS invoice_sources (
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    file_name TEXT NOT NULL,
    layout TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (company_id, invoice_id)
);
CREATE TABLE IF NOT EXISTS bank_txns (
    company_id TEXT NOT NULL REFERENCES companies(company_id),
    txn_id TEXT NOT NULL,
    txn_date TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    counterparty TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (company_id, txn_id)
);
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_id TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    topic TEXT NOT NULL,
    companies TEXT NOT NULL,          -- JSON 数组，如 ["C001","C002"]
    effective_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    body TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}', -- 程序可执行的参数，如用途必填金额门槛
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'event')),
    content TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'done')),
    goal TEXT,
    month TEXT,
    notes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 初赛：每个用户在每家企业最多一个未完成任务
CREATE UNIQUE INDEX IF NOT EXISTS one_open_task
    ON tasks(user_id, company_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS open_items (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'dropped')),
    rule_ref TEXT,
    detail TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, invoice_id, kind)
);
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    company_id TEXT,                   -- NULL 表示用户明确要求跨企业通用
    content TEXT,                      -- 删除后清空正文
    status TEXT NOT NULL CHECK (status IN ('active', 'deleted')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    op_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT,
    intended_json TEXT,                -- 打算写入的字段与值，用于中断后的回读核对
    status TEXT NOT NULL CHECK (status IN ('pending', 'verified', 'mismatch', 'failed')),
    created_at TEXT NOT NULL,
    recovered_at TEXT                  -- 由启动恢复流程补做回读核验的时间
);
-- 原话不足以直接写入时由程序记下的待确认修改：绑定会话、用户、企业、发票、日期与字段值，
-- 只有紧接着的下一句用户原话明确确认，才能按这里记下的内容写入
CREATE TABLE IF NOT EXISTS pending_changes (
    change_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    after_message_id INTEGER NOT NULL, -- 提出时最近一条用户消息
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    final_reply TEXT,
    status TEXT NOT NULL,
    model TEXT,
    selected TEXT NOT NULL DEFAULT '{}', -- 选用的记忆、规则
    usage TEXT NOT NULL DEFAULT '{}',
    anomalies TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


# 旧库升级：只追加列，不改已有数据
MIGRATIONS = {"operations": {"intended_json": "TEXT", "recovered_at": "TEXT"}}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # 显式 BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
