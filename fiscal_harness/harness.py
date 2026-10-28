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
from .runlog import RunLog, redact

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


def run_turn(conn: sqlite3.Connection, llm: LLM, settings: Settings, session_id: str, user_message: str,
             attachments: list | None = None) -> TurnResult:
    """attachments：随这条消息上传、已由程序解析校验的发票（invoice_files.ParsedInvoice），暂存待用户要求导入。"""
    scope = session_scope(conn, session_id, settings.today)
    scope.run_id = "R-" + uuid.uuid4().hex[:12]
    if attachments:
        user_message += tools.ATTACH_MARK + "、".join(a.file_name for a in attachments)
    log = RunLog(conn, scope.run_id, session_id, scope.user_id, scope.company_id, user_message, llm.model)
    cur = conn.execute("INSERT INTO messages (session_id, company_id, role, content, run_id, created_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (session_id, scope.company_id, "user", redact(user_message), scope.run_id, _now()))
    if attachments:
        staged = tools.stage_uploads(conn, scope, cur.lastrowid, attachments)
        log.step("uploads", {"staged": dict(zip(staged, (a.file_name for a in attachments)))})
    box = tools.Toolbox(conn, scope)

    system, selected = context.build_system(conn, scope, settings.variant)
    messages = [{"role": "system", "content": system}] + context.history(conn, scope, settings.history_chars)
    usage_total: dict[str, int] = {}
    calls: list[dict] = []
    reply, status = "", "completed"
    progress: list[str] = []      # 本轮已完成的步骤，切换企业后由程序带入，避免丢失进度
    first_reply = None            # 引用核对前的回复：重答没能完成时仍用它
    try:
        for step in range(settings.max_steps):
            specs = tools.tool_specs(settings.variant)
            log.step("llm_input", {"messages": messages, "tools": [t["function"]["name"] for t in specs]})
            out = llm.chat(messages, specs)
            for k, v in out.usage.items():
                if isinstance(v, int):
                    usage_total[k] = usage_total.get(k, 0) + v
            log.step("llm_output", {"message": out.message, "usage": out.usage, "model": out.model})
            messages.append(out.message)
            if not out.message.get("tool_calls"):
                reply = out.message.get("content", "").strip()
                bad = _check_citations(reply, scope.rule_refs)
                if bad and first_reply is None and step + 2 < settings.max_steps:
                    # 引用了本轮工具没有返回的规则版本：请模型先取得条款或去掉该引用，只重答一次
                    first_reply = reply
                    log.step("citation_check", {"anomalies": bad})
                    messages.append({"role": "user", "content": "[程序核对] " + "；".join(bad) + "。请先查到该规则条款"
                                     "再引用；查不到就去掉该引用并说明资料不足。直接给出完整回复，不要提到这条核对。"})
                    continue
                break
            switched_from, batch = None, []
            for call in out.message["tool_calls"]:
                name, args = call["function"]["name"], call["function"]["arguments"]
                company_before = scope.company_id
                if switched_from:
                    # 同一批次里排在切换之后的调用是按旧企业决定的，不执行，交回模型按新企业重新判断
                    result = {"ok": False, "error": {"code": "SKIPPED_AFTER_SWITCH", "message":
                              f"本批次已从 {switched_from} 切换到 {scope.company_id}，此调用未执行。请按新企业重新决定是否需要。"}}
                else:
                    result = tools.dispatch(box, name, args, settings.variant)
                    if name == "switch_company" and scope.company_id != company_before:
                        switched_from = company_before
                calls.append({"tool": name, "args": args, "ok": result["ok"],
                              "error": result.get("error", {}).get("code")})
                if result["ok"] and name in PROGRESS_TOOLS:
                    progress.append(f"- {company_before} {name} {_args_text(args)} → "
                                    + json.dumps(result["data"], ensure_ascii=False)[:PROGRESS_CHARS])
                log.step("tool", {"name": name, "arguments": args, "company_id": company_before, "result": result})
                batch.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            if switched_from:
                # 切换后重建系统输入与对话历史，旧企业的规则、记忆和本轮此前的查询结果不再带入
                conn.execute("UPDATE messages SET company_id=? WHERE run_id=? AND role='user'",
                             (scope.company_id, scope.run_id))
                system, new_selected = context.build_system(conn, scope, settings.variant)
                if progress:
                    # 用户本轮要求的跨企业步骤：之前企业的结果由程序原样带入，只用于本轮回复，不能用于当前企业的操作
                    system += ("\n\n## 本轮已完成的步骤（程序记录，按企业标注）\n" + "\n".join(progress)
                               + "\n这些步骤已经完成，不要切回去重复查询。回复时分别注明各企业的结果；"
                               "其他企业的数据不能用于当前企业的查询或修改。")
                selected = {**new_selected, "before_switch": {switched_from: selected}}
                messages = ([{"role": "system", "content": system}]
                            + context.history(conn, scope, settings.history_chars) + [out.message])
            messages += batch
        else:
            if first_reply is not None:
                reply = first_reply
            else:
                status = "step_limit"
                reply = "这一轮处理步骤较多，已暂停。已完成的查询和保存都已记录，请告诉我是否继续。"
    except LLMError as e:
        status = "llm_error"
        reply = f"模型服务暂时不可用，这一轮没有完成。（{e}）已执行的操作以运行记录为准。"
        log.step("error", {"message": str(e)})
    except tools.SimulatedCrash:
        raise  # 故障演示：保留“提交后、回读前中断”的现场，由下次启动恢复
    except Exception as e:  # 程序缺陷也要把这一轮收尾，不能让运行记录停在 running
        status = "error"
        reply = "处理时出现程序错误，这一轮没有完成。已执行的保存以运行记录和数据核对为准。"
        log.step("error", {"type": type(e).__name__, "message": str(e)})

    anomalies = _check_citations(reply, scope.rule_refs)
    # 本轮如切换了企业，用户消息和回复都归到最终企业，旧企业的对话不再带入新企业
    conn.execute("UPDATE messages SET company_id=? WHERE run_id=? AND role='user'", (scope.company_id, scope.run_id))
    conn.execute("INSERT INTO messages (session_id, company_id, role, content, run_id, created_at) VALUES (?,?,?,?,?,?)",
                 (session_id, scope.company_id, "assistant", redact(reply), scope.run_id, _now()))
    selected["rules_cited_by_tools"] = [f"{r['rule_id']} {r['version']}" for r in scope.rule_refs]
    log.finish(reply=reply, status=status, company_id=scope.company_id, selected=selected, usage=usage_total,
               anomalies=anomalies)
    return TurnResult(scope.run_id, reply, status, scope.company_id, calls, anomalies)


PROGRESS_TOOLS = {"summarize_month", "query_invoices", "query_bank", "check_purpose", "update_invoice"}
PROGRESS_CHARS = 1500


def _args_text(args: str | dict) -> str:
    try:
        data = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError:
        return str(args)
    return json.dumps(data, ensure_ascii=False)


def _check_citations(reply: str, refs: list[dict]) -> list[str]:
    """回复里出现的《规则名》x.x 版，必须是本轮工具实际返回过的版本，否则记为异常。"""
    known = {(r["name"], r["version"]) for r in refs}
    out = []
    for name, version in CITATION.findall(reply):
        if (name, version) not in known:
            out.append(f"回复引用了《{name}》{version} 版，但本轮工具没有返回该版本")
    return out
