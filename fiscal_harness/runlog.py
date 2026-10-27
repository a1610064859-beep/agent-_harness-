"""运行记录：每轮保存完整模型输入、模型输出、工具调用与结果、选用的记忆与规则。不记录密钥。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime

_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{16,}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def redact(text: str) -> str:
    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _KEY_PATTERN.sub("[REDACTED]", text)


class RunLog:
    def __init__(self, conn: sqlite3.Connection, run_id: str, session_id: str, user_id: str, company_id: str,
                 user_message: str, model: str):
        self.conn, self.run_id, self.seq = conn, run_id, 0
        conn.execute("INSERT INTO runs (run_id, session_id, user_id, company_id, user_message, status, model, "
                     "started_at) VALUES (?,?,?,?,?,?,?,?)",
                     (run_id, session_id, user_id, company_id, redact(user_message), "running", model, _now()))

    def step(self, kind: str, payload) -> None:
        self.seq += 1
        self.conn.execute("INSERT INTO run_steps (run_id, seq, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (self.run_id, self.seq, kind, redact(json.dumps(payload, ensure_ascii=False)), _now()))

    def finish(self, *, reply: str, status: str, company_id: str, selected: dict, usage: dict,
               anomalies: list[str]) -> None:
        self.conn.execute("UPDATE runs SET final_reply=?, status=?, company_id=?, selected=?, usage=?, anomalies=?, "
                          "ended_at=? WHERE run_id=?",
                          (redact(reply), status, company_id, redact(json.dumps(selected, ensure_ascii=False)),
                           json.dumps(usage), redact(json.dumps(anomalies, ensure_ascii=False)), _now(), self.run_id))


def load(conn: sqlite3.Connection, run_id: str) -> dict | None:
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not run:
        return None
    out = {k: redact(v) if isinstance(v, str) else v for k, v in dict(run).items()}  # 旧库中的记录导出时也过滤
    for k in ("selected", "usage", "anomalies"):
        out[k] = json.loads(out[k])
    out["steps"] = [{"seq": s["seq"], "kind": s["kind"], "payload": json.loads(redact(s["payload"]))}
                    for s in conn.execute("SELECT * FROM run_steps WHERE run_id=? ORDER BY seq", (run_id,))]
    return out
