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

_VERSION = (r"\s*(?:[（(]\s*)?(?P<pre>(?:版本|version|v)\s*)?(?P<ver>\d+(?:\.\d+)+)(?![\d.])"
            r"(?P<suf>\s*版)?")
CITATION = re.compile(r"《(?P<name>[^》]+)》" + _VERSION, re.I)
ID_CITATION = re.compile(r"(?<![A-Za-z0-9])(?P<name>R\d{3})(?!\d)\s*的?" + _VERSION, re.I)
_UNIT_AFTER = set("元%％万千年月日号天张笔")


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
    if attachments and not settings.file_import:
        raise ValueError("未开启发票文件导入（FH_FILE_IMPORT=1），不能随消息附带文件")
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

    system, selected = context.build_system(conn, scope, settings.variant, settings.file_import)
    messages = [{"role": "system", "content": system}] + context.history(conn, scope, settings.history_chars)
    usage_total: dict[str, int] = {}
    calls: list[dict] = []
    reply, status = "", "completed"
    progress: list[str] = []      # 本轮已完成的步骤，切换企业后由程序带入，避免丢失进度
    first_reply = None            # 引用核对前的回复：重答没能完成时仍用它
    forgotten_turn: list[tuple[str, str]] = []  # 本轮删除的偏好：结束时从回复与本轮记录中清除
    try:
        for step in range(settings.max_steps):
            specs = tools.tool_specs(settings.variant, settings.file_import)
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
                    result = tools.dispatch(box, name, args, settings.variant, settings.file_import)
                    if name == "switch_company" and scope.company_id != company_before:
                        switched_from = company_before
                calls.append({"tool": name, "args": args, "ok": result["ok"],
                              "error": result.get("error", {}).get("code")})
                if result["ok"] and name in PROGRESS_TOOLS:
                    progress.append(f"- {company_before} {name} {_args_text(args)} → "
                                    + json.dumps(result["data"], ensure_ascii=False)[:PROGRESS_CHARS])
                log.step("tool", {"name": name, "arguments": args, "company_id": company_before, "result": result})
                batch.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            forgotten = list(box.forgotten_memories)
            forgotten_turn += forgotten
            memory_changed = bool(forgotten) or box.memory_revised
            box.memory_revised = False
            if forgotten:
                # 之前构建的 system、历史和工具消息也可能带旧偏好；不能在删除后再次发送或记入日志。
                for text, placeholder in forgotten:
                    messages = tools.scrub_memory_text(messages, text, placeholder)
                    batch = tools.scrub_memory_text(batch, text, placeholder)
                box.forgotten_memories.clear()
            if switched_from:
                # 切换后重建系统输入与对话历史，旧企业的规则、记忆和本轮此前的查询结果不再带入
                conn.execute("UPDATE messages SET company_id=? WHERE run_id=? AND role='user'",
                             (scope.company_id, scope.run_id))
                system, new_selected = context.build_system(conn, scope, settings.variant, settings.file_import)
                if progress:
                    # 用户本轮要求的跨企业步骤：之前企业的结果由程序原样带入，只用于本轮回复，不能用于当前企业的操作
                    system += ("\n\n## 本轮已完成的步骤（程序记录，按企业标注）\n" + "\n".join(progress)
                               + "\n这些步骤已经完成，不要切回去重复查询。回复时分别注明各企业的结果；"
                               "其他企业的数据不能用于当前企业的查询或修改。")
                selected = {**new_selected, "before_switch": {switched_from: selected}}
                messages = ([{"role": "system", "content": system}]
                            + context.history(conn, scope, settings.history_chars) + [out.message])
                for text, placeholder in forgotten_turn:
                    messages[1:] = tools.scrub_memory_text(messages[1:], text, placeholder)
            elif memory_changed:
                system, new_selected = context.build_system(conn, scope, settings.variant, settings.file_import)
                selected = {**selected, **new_selected}
                messages[0] = {"role": "system", "content": system}
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
    if anomalies and status == "completed":
        # 重答后仍引用了本轮没有查到的规则版本：结论未经证实，原稿只留在运行记录里，不发给用户，也不报完成
        status = "needs_review"
        log.step("held_reply", {"reply": reply, "anomalies": anomalies})
        reply = HELD_REPLY
    for text, placeholder in forgotten_turn:
        # 模型可能在删除后的回复里复述原文（“已删除偏好：……”），回复和其后记录的步骤也要清除
        reply = tools.scrub_memory_text(reply, text, placeholder)
    # 本轮如切换了企业，用户消息和回复都归到最终企业，旧企业的对话不再带入新企业
    conn.execute("UPDATE messages SET company_id=? WHERE run_id=? AND role='user'", (scope.company_id, scope.run_id))
    conn.execute("INSERT INTO messages (session_id, company_id, role, content, run_id, created_at) VALUES (?,?,?,?,?,?)",
                 (session_id, scope.company_id, "assistant", redact(reply), scope.run_id, _now()))
    selected["rules_cited_by_tools"] = [f"{r['rule_id']} {r['version']}" for r in scope.rule_refs]
    log.finish(reply=reply, status=status, company_id=scope.company_id, selected=selected, usage=usage_total,
               anomalies=anomalies)
    for text, placeholder in forgotten_turn:
        tools.scrub_run(conn, scope.run_id, text, placeholder)
    return TurnResult(scope.run_id, reply, status, scope.company_id, calls, anomalies)


HELD_REPLY = ("这次的回答引用了本轮没有查到的规则版本，未经核对，为避免给出没有依据的结论，暂不发出。"
              "本轮已执行的查询和保存以运行记录为准。请让我重新查询相关规则后再回答。")
PROGRESS_TOOLS = {"summarize_month", "query_invoices", "query_bank", "check_purpose", "update_invoice"}
PROGRESS_CHARS = 1500


def _args_text(args: str | dict) -> str:
    try:
        data = json.loads(args) if isinstance(args, str) else args
    except json.JSONDecodeError:
        return str(args)
    return json.dumps(data, ensure_ascii=False)


def _check_citations(reply: str, refs: list[dict]) -> list[str]:
    """按规则名或编号核对版本，兼容 v 前缀、括号和省略“版”的常用写法。"""
    known = {(r["name"], r["version"]) for r in refs}
    known_ids = {(r["rule_id"].upper(), r["version"]) for r in refs if r.get("rule_id")}
    out = []
    for name, version in _cited(CITATION, reply):
        if (name, version) not in known:
            out.append(f"回复引用了《{name}》{version} 版，但本轮工具没有返回该版本")
    for rule_id, version in _cited(ID_CITATION, reply):
        if (rule_id.upper(), version) not in known_ids:
            out.append(f"回复引用了{rule_id.upper()} {version} 版，但本轮工具没有返回该版本")
    return list(dict.fromkeys(out))


def _cited(pattern: re.Pattern, reply: str):
    """带“版/版本/v”标记的一律按版本核对；没有标记的数字只有像版本号时才算，
    避免把“R002 1000.00元”“R002 2026.09 起”这类金额、日期误判为引用。"""
    for m in pattern.finditer(reply):
        version = m["ver"]
        if not (m["pre"] or m["suf"]):
            nxt = reply[m.end():].lstrip()[:1]
            if len(version.split(".")[0]) > 2 or nxt in _UNIT_AFTER:
                continue
        yield m["name"], version
