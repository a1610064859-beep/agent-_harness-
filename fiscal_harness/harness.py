"""统一运行循环：三个场景共用。模型决定查询、追问、调用工具或结束；程序负责守卫、执行与记录。"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import context, tools
from .config import Settings
from .llm import LLM, LLMError
from .runlog import RunLog

CITATION = re.compile(r"《([^》]+)》\s*([\d.]+)\s*版")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class TurnResult:
    run_id: str
    reply: str
    status: str
    company_id: str
    tool_calls: list[dict]
    anomalies: list[str]


def start_session(conn: sqlite3.Connection, user_id: str, company_id: str) -> str:
    if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
        raise ValueError(f"没有用户 {user_id}")
    if not conn.execute("SELECT 1 FROM companies WHERE company_id=?", (company_id,)).fetchone():
        raise ValueError(f"没有企业 {company_id}")
    sid = "S-" + uuid.uuid4().hex[:10]
    conn.execute("INSERT INTO sessions (session_id, user_id, company_id, created_at) VALUES (?,?,?,?)",
                 (sid, user_id, company_id, _now()))
    return sid


def session_scope(conn: sqlite3.Connection, session_id: str, today) -> tools.Scope:
    row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        raise ValueError(f"没有会话 {session_id}")
    return tools.Scope(session_id, row["user_id"], row["company_id"], today)


def run_turn(conn: sqlite3.Connection, llm: LLM, settings: Settings, session_id: str, user_message: str) -> TurnResult:
    scope = session_scope(conn, session_id, settings.today)
    scope.run_id = "R-" + uuid.uuid4().hex[:12]
    log = RunLog(conn, scope.run_id, session_id, scope.user_id, scope.company_id, user_message, llm.model)
    conn.execute("INSERT INTO messages (session_id, company_id, role, content, run_id, created_at) VALUES (?,?,?,?,?,?)",
                 (session_id, scope.company_id, "user", user_message, scope.run_id, _now()))
    box = tools.Toolbox(conn, scope)

    system, selected = context.build_system(conn, scope)
    messages = [{"role": "system", "content": system}] + context.history(conn, scope, settings.history_chars)
    usage_total: dict[str, int] = {}
    calls: list[dict] = []
    reply, status = "", "completed"
    try:
        for _ in range(settings.max_steps):
            log.step("llm_input", {"messages": messages, "tools": [t["function"]["name"] for t in tools.TOOL_SPECS]})
            out = llm.chat(messages, tools.TOOL_SPECS)
            for k, v in out.usage.items():
                if isinstance(v, int):
                    usage_total[k] = usage_total.get(k, 0) + v
            log.step("llm_output", {"message": out.message, "usage": out.usage, "model": out.model})
            messages.append(out.message)
            if not out.message.get("tool_calls"):
                reply = out.message.get("content", "").strip()
                break
            for call in out.message["tool_calls"]:
                name, args = call["function"]["name"], call["function"]["arguments"]
                company_before = scope.company_id
                result = tools.dispatch(box, name, args)
                calls.append({"tool": name, "args": args, "ok": result["ok"],
                              "error": result.get("error", {}).get("code")})
                log.step("tool", {"name": name, "arguments": args, "company_id": company_before, "result": result})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        else:
            status = "step_limit"
            reply = "这一轮处理步骤较多，已暂停。已完成的查询和保存都已记录，请告诉我是否继续。"
    except LLMError as e:
        status = "llm_error"
        reply = f"模型服务暂时不可用，这一轮没有完成。（{e}）已执行的操作以运行记录为准。"
        log.step("error", {"message": str(e)})

    anomalies = _check_citations(reply, scope.rule_refs)
    # 本轮如切换了企业，用户消息和回复都归到最终企业，旧企业的对话不再带入新企业
    conn.execute("UPDATE messages SET company_id=? WHERE run_id=? AND role='user'", (scope.company_id, scope.run_id))
    conn.execute("INSERT INTO messages (session_id, company_id, role, content, run_id, created_at) VALUES (?,?,?,?,?,?)",
                 (session_id, scope.company_id, "assistant", reply, scope.run_id, _now()))
    selected["rules_cited_by_tools"] = [f"{r['rule_id']} {r['version']}" for r in scope.rule_refs]
    log.finish(reply=reply, status=status, company_id=scope.company_id, selected=selected, usage=usage_total,
               anomalies=anomalies)
    return TurnResult(scope.run_id, reply, status, scope.company_id, calls, anomalies)


def _check_citations(reply: str, refs: list[dict]) -> list[str]:
    """回复里出现的《规则名》x.x 版，必须是本轮工具实际返回过的版本，否则记为异常。"""
    known = {(r["name"], r["version"]) for r in refs}
    out = []
    for name, version in CITATION.findall(reply):
        if (name, version) not in known:
            out.append(f"回复引用了《{name}》{version} 版，但本轮工具没有返回该版本")
    return out
