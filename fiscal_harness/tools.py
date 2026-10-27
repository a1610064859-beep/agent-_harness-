"""模型可调用的工具与程序守卫。

- 用户、企业由会话绑定后注入，模型参数里没有这两项，无法越权改范围。
- 金额、合计、用途检查全部由程序计算；规则版本由 knowledge.resolve 解析。
- 写入只开放发票用途与备注，需本会话用户原话作授权凭证，写后回读核验，同一修改幂等。
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from . import knowledge, memory
from .money import fmt

LEVEL_TEXT = {"write": "可查看并修改", "read": "仅查看", "none": "无权访问"}
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
          "十一": 11, "十二": 12}
# 只允许本次初赛范围内的月份做用途检查（《业务规则》：初赛仅检查 2026 年 9 月发票用途）
PURPOSE_CHECK_MONTHS = ("2026-09",)


class SimulatedCrash(RuntimeError):
    """故障注入：FH_FAULT=crash_after_commit 时，在写入提交后、回读前模拟进程中断。"""


class ToolError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


@dataclass
class Scope:
    session_id: str
    user_id: str
    company_id: str
    today: date
    run_id: str | None = None
    rule_refs: list[dict] = field(default_factory=list)   # 本轮工具返回过的规则版本
    memory_refs: list[str] = field(default_factory=list)  # 本轮选用的记忆


# 用户在来源原话里明确表示暂不保存：守卫只用来拒绝写入，不用来判断意图或放行
SAVE_NEGATION = re.compile(r"(别|不要|不用|不必|勿|先不|暂不|暂时不|还不)(急着?)?(保存|写入|提交)")


def _norm(text: str) -> str:
    return re.sub(r"[\s\"'“”‘’「」『』《》:：,，.。!！?？;；、]", "", text or "")


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def mentioned_months(texts: list[str], today: date) -> set[str]:
    """从用户原话里找出明确提到的月份，用于判断模型传入的月份是否有出处（不用于理解意图）。"""
    found = set()
    for text in texts:
        for y, m in re.findall(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})", text):
            found.add(f"{y}-{int(m):02d}")
        for m in re.findall(r"(?<![\d年/.-])(\d{1,2})\s*月", text):
            if 1 <= int(m) <= 12:
                found.add(_nearest_year_month(int(m), today))
        for m in re.findall(r"(十二|十一|十|一|二|三|四|五|六|七|八|九)\s*月", text):
            found.add(_nearest_year_month(CN_NUM[m], today))
        if re.search(r"上个?月", text):
            prev = today.replace(day=1).toordinal() - 1
            found.add(date.fromordinal(prev).strftime("%Y-%m"))
        if re.search(r"本月|这个月|当月", text):
            found.add(today.strftime("%Y-%m"))
    return found


def _nearest_year_month(month: int, today: date) -> str:
    year = today.year if month <= today.month else today.year - 1
    return f"{year}-{month:02d}"


class Toolbox:
    def __init__(self, conn: sqlite3.Connection, scope: Scope):
        self.conn, self.scope = conn, scope

    # ---------- 范围与权限 ----------

    @property
    def level(self) -> str:
        row = self.conn.execute("SELECT level FROM permissions WHERE user_id=? AND company_id=?",
                                (self.scope.user_id, self.scope.company_id)).fetchone()
        return row["level"] if row else "none"

    def _require(self, need: str) -> None:
        level = self.level
        if level == "none" or (need == "write" and level != "write"):
            raise ToolError("FORBIDDEN", f"用户 {self.scope.user_id} 对企业 {self.scope.company_id} 的权限是"
                                         f"“{LEVEL_TEXT[level]}”，不能{'修改' if need == 'write' else '查看'}。"
                                         "这不是“没有记录”，不能回答为零。")

    def _scope_user_messages(self) -> list[sqlite3.Row]:
        """可作为出处的用户原话：本会话中当前企业最近连续的一段（切换企业前的话不再作数），本轮用户消息总算在内。"""
        rows = self.conn.execute("SELECT id, role, company_id, content FROM messages WHERE session_id=? ORDER BY id DESC",
                                 (self.scope.session_id,)).fetchall()
        out, latest_seen = [], False
        for r in rows:
            if r["role"] == "user" and not latest_seen:
                latest_seen = True  # 本轮的话：可能正是它要求切换企业
                out.append(r)
                continue
            if r["company_id"] != self.scope.company_id:
                break
            if r["role"] == "user":
                out.append(r)
        return out[::-1]

    def _session_user_texts(self) -> list[str]:
        return [r["content"] for r in self._scope_user_messages()]

    def _named_invoices(self, text: str) -> set[str]:
        norm = _norm(text).upper()
        ids = [r["invoice_id"] for r in self.conn.execute("SELECT invoice_id FROM invoices WHERE company_id=?",
                                                          (self.scope.company_id,))]
        return {i for i in ids if i in norm or ("-" in i and i.split("-", 1)[1] in norm)}

    def _authorize_write(self, invoice_id: str, user_quote: str, changes: dict) -> int:
        """修改授权绑定到当前企业、发票、字段值和一条来源原话；返回来源消息 id。"""
        msgs = self._scope_user_messages()
        quote = _norm(user_quote)
        sources = [m for m in msgs if quote and quote in _norm(m["content"])]
        if not sources:
            raise ToolError("NOT_AUTHORIZED", "user_quote 必须逐字摘自用户在本次会话、当前企业下要求修改的一句原话。"
                                              "之前会话的草稿、切换企业前的话或模型自己的推断都不能作为修改授权，请向用户确认。")
        src = sources[-1]
        if SAVE_NEGATION.search(_norm(src["content"])):
            raise ToolError("NOT_AUTHORIZED", "用户在这句原话里表示暂不保存，不能据此修改。请等用户明确要求保存。")
        basis, named = [src], self._named_invoices(src["content"])
        if not named:
            # 原话没点名发票（如“可以了，保存吧”）：对象取自它之前最近一条点名了发票的用户原话
            earlier = [m for m in msgs if m["id"] < src["id"] and self._named_invoices(m["content"])]
            if earlier:
                basis.append(earlier[-1])
                named = self._named_invoices(earlier[-1]["content"])
        if invoice_id not in named:
            raise ToolError("INVOICE_NOT_REQUESTED", f"用户原话要求修改的是 {'、'.join(sorted(named)) or '（未点名发票）'}，"
                                                     f"不是 {invoice_id}。请按用户点名的发票操作，或向用户确认。",
                            named_invoices=sorted(named))
        text = _norm("".join(m["content"] for m in basis))
        for k, v in changes.items():
            if _norm(v) not in text:
                raise ToolError("CONTENT_NOT_FROM_USER", f"{'用途' if k == 'purpose' else '备注'}“{v}”没有出现在用户"
                                                         "要求修改的原话中。请使用用户给出的原文，不能根据销售方或项目名称补写。")
        return src["id"]

    def _check_month(self, month: str) -> str:
        if not month or not MONTH_RE.match(month):
            raise ToolError("BAD_MONTH", "月份必须是 YYYY-MM 格式。")
        task = memory.open_task(self.conn, self.scope.user_id, self.scope.company_id)
        allowed = mentioned_months(self._session_user_texts(), self.scope.today)
        if task and task["month"]:
            allowed.add(task["month"])
        if month not in allowed:
            raise ToolError("MONTH_UNCONFIRMED", f"用户在本会话中没有说明要查 {month}，任务记忆中也没有该月份。"
                                                 "请先向用户确认月份，不能默认使用当前月份。",
                            known_months=sorted(allowed))
        return month

    def _task(self) -> sqlite3.Row:
        return memory.ensure_task(self.conn, self.scope.user_id, self.scope.company_id)

    def _cite(self, ref: dict | None) -> dict | None:
        if ref and ref not in self.scope.rule_refs:
            self.scope.rule_refs.append(ref)
        return ref

    def _resolve_one(self, as_of: date, **kw) -> knowledge.Resolved | None:
        res = knowledge.resolve(self.conn, self.scope.company_id, as_of=as_of, **kw)
        return res[0] if res else None

    # ---------- 查询 ----------

    def query_invoices(self, month: str, invoice_ids: list[str] | None = None,
                       missing_purpose_only: bool = False) -> dict:
        self._require("read")
        self._check_month(month)
        start, end = _month_bounds(month)
        rows = self.conn.execute("SELECT * FROM invoices WHERE company_id=? AND issue_date BETWEEN ? AND ? "
                                 "ORDER BY invoice_id", (self.scope.company_id, start, end)).fetchall()
        if invoice_ids:
            wanted = {i.strip().upper() for i in invoice_ids}
            unknown = wanted - {r["invoice_id"] for r in rows}
            rows = [r for r in rows if r["invoice_id"] in wanted]
        else:
            unknown = set()
        if missing_purpose_only:
            rows = [r for r in rows if not (r["purpose"] or "").strip()]
        memory.update_task(self.conn, self._task()["task_id"], month=month)
        return {"company_id": self.scope.company_id, "month": month, "count": len(rows),
                "listed_total": fmt(sum(r["amount_cents"] for r in rows)),
                "invoices": [_invoice_view(r) for r in rows],
                "not_found_in_month": sorted(unknown),
                "note": None if rows else "查询成功，没有匹配的发票记录。"}

    def query_bank(self, month: str, direction: str | None = None) -> dict:
        self._require("read")
        self._check_month(month)
        if direction not in (None, "income", "expense"):
            raise ToolError("BAD_ARG", "direction 只能是 income 或 expense。")
        start, end = _month_bounds(month)
        sql = "SELECT * FROM bank_txns WHERE company_id=? AND txn_date BETWEEN ? AND ?"
        args = [self.scope.company_id, start, end]
        if direction:
            sql, args = sql + " AND direction=?", args + [direction]
        rows = self.conn.execute(sql + " ORDER BY txn_date, txn_id", args).fetchall()
        memory.update_task(self.conn, self._task()["task_id"], month=month)
        return {"company_id": self.scope.company_id, "month": month, "count": len(rows),
                "transactions": [{"txn_id": r["txn_id"], "date": r["txn_date"],
                                  "direction": "收入" if r["direction"] == "income" else "支出",
                                  "amount": fmt(r["amount_cents"]), "counterparty": r["counterparty"],
                                  "note": r["note"]} for r in rows],
                "rule": self._cite(self._resolve_one(self.scope.today, rule_id="R004").ref()),
                "note": None if rows else "查询成功，没有匹配的银行记录。"}

    def summarize_month(self, month: str) -> dict:
        self._require("read")
        self._check_month(month)
        start, end = _month_bounds(month)
        cid = self.scope.company_id
        inv = self.conn.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s FROM invoices "
                                "WHERE company_id=? AND issue_date BETWEEN ? AND ?", (cid, start, end)).fetchone()
        bank = {r["direction"]: r for r in self.conn.execute(
            "SELECT direction, COUNT(*) n, COALESCE(SUM(amount_cents),0) s FROM bank_txns "
            "WHERE company_id=? AND txn_date BETWEEN ? AND ? GROUP BY direction", (cid, start, end))}
        income, expense = bank.get("income"), bank.get("expense")
        as_of = date.fromisoformat(end)
        memory.update_task(self.conn, self._task()["task_id"], month=month)
        empty = inv["n"] == 0 and not bank
        return {"company_id": cid, "month": month, "unit": "元",
                "invoice_count": inv["n"], "invoice_total": fmt(inv["s"]),
                "bank_income_count": income["n"] if income else 0,
                "bank_income_total": fmt(income["s"] if income else 0),
                "bank_expense_count": expense["n"] if expense else 0,
                "bank_expense_total": fmt(expense["s"] if expense else 0),
                "calculation": "发票总额=全部发票价税合计之和；银行收入、支出按方向分别相加，互不抵减",
                "rules": [self._cite(r.ref()) for r in knowledge.resolve(self.conn, cid, as_of=as_of)
                          if r.chosen and r.chosen["topic"] in ("amount", "bank")],
                "note": "查询成功，该月没有发票和银行记录，合计为 0.00 元。" if empty else None}

    def check_purpose(self, month: str) -> dict:
        self._require("read")
        self._check_month(month)
        status = purpose_status(self.conn, self.scope.company_id, month)
        ref = self._cite(status["rule"])
        task = self._task()
        memory.update_task(self.conn, task["task_id"], month=month)
        label = f'{ref["rule_id"]} {ref["version"]}'
        previous = {r["rule_ref"] for r in memory.items(self.conn, task["task_id"])
                    if r["state"] == "pending" and r["rule_ref"]} - {label}
        memory.sync_purpose_items(self.conn, task["task_id"], self.scope.company_id,
                                  [r["invoice_id"] for r in status["missing_required"]], status["filled"], label)
        status.pop("filled")
        return {**status, "rule_changed_since_last_check": sorted(previous) or None,
                "hint": "空白或只有空格视为未填写；备注不能代替用途；不能根据销售方或项目名称推测用途。"}

    def search_rules(self, query: str, month: str | None = None) -> dict:
        as_of = date.fromisoformat(_month_bounds(self._check_month(month))[1]) if month else self.scope.today
        result = knowledge.search(self.conn, self.scope.company_id, query, as_of=as_of)
        for s in result["snippets"]:
            self._cite({k: s[k] for k in ("rule_id", "name", "version", "effective_from")})
        if not result["snippets"]:
            result["note"] = "适用规则中没有找到相关内容，请如实告诉用户资料不足。"
        return result

    def get_rule(self, rule_id: str, month: str | None = None) -> dict:
        as_of = date.fromisoformat(_month_bounds(self._check_month(month))[1]) if month else self.scope.today
        rule = knowledge.full_text(self.conn, self.scope.company_id, rule_id.strip().upper(), as_of=as_of)
        if not rule:
            raise ToolError("NO_RULE", f"{rule_id} 对当前企业在 {as_of} 没有有效版本。")
        self._cite({k: rule[k] for k in ("rule_id", "name", "version", "effective_from")})
        return rule

    def list_rule_documents(self) -> dict:
        """仅用于对比实验的对照组：不做适用性解析，原样返回与当前企业相关的全部规则版本。"""
        rows = self.conn.execute("SELECT * FROM rule_versions ORDER BY rule_id, effective_from").fetchall()
        docs = [{"rule_id": r["rule_id"], "name": r["name"], "version": r["version"], "status": r["status"],
                 "effective_from": r["effective_from"], "companies": json.loads(r["companies"]), "text": r["body"]}
                for r in rows if self.scope.company_id in json.loads(r["companies"])]
        for d in docs:
            self._cite({k: d[k] for k in ("rule_id", "name", "version", "effective_from")})
        return {"documents": docs}

    # ---------- 写入 ----------

    def update_invoice(self, invoice_id: str, user_quote: str, purpose: str | None = None,
                       remark: str | None = None) -> dict:
        self._require("write")
        invoice_id = invoice_id.strip().upper()
        changes = {k: v.strip() for k, v in (("purpose", purpose), ("remark", remark)) if v is not None}
        if not changes:
            raise ToolError("BAD_ARG", "只能修改用途（purpose）或备注（remark），至少提供一项。")
        if any(not v for v in changes.values()):
            raise ToolError("BAD_ARG", "用途和备注只能补充或更正为非空内容。")
        source_id = self._authorize_write(invoice_id, user_quote, changes)
        cid = self.scope.company_id
        # 同一句原话的重试才算重复；用户之后的新要求（包括改回旧值）是新的操作
        key = hashlib.sha256(json.dumps([self.scope.session_id, cid, invoice_id, changes, source_id], sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest()
        prior = self.conn.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
        if prior:
            current = self._read_invoice(cid, invoice_id)
            return {"duplicate": True, "op_id": prior["op_id"], "status": prior["status"],
                    "current_matches_request": all((current[k] or "") == v for k, v in changes.items()),
                    "message": "用户这句原话要求的修改已经执行过，没有再次写入。",
                    "current": _invoice_view(current)}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            before = self._read_invoice(cid, invoice_id)
            if before is None:
                raise ToolError("NOT_FOUND", f"企业 {cid} 没有发票 {invoice_id}。发票由企业编号和发票编号共同确定，"
                                             "不能修改其他企业的同编号发票。")
            if all((before[k] or "") == v for k, v in changes.items()):
                self.conn.execute("ROLLBACK")
                return {"no_change": True, "message": "当前内容已经是这个值，没有重复修改。",
                        "current": _invoice_view(before)}
            op_id = "OP-" + uuid.uuid4().hex[:10]
            sets = ", ".join(f"{k}=?" for k in changes)
            cur = self.conn.execute(f"UPDATE invoices SET {sets}, version=version+1, updated_at=? "
                                    "WHERE company_id=? AND invoice_id=? AND version=?",
                                    (*changes.values(), _now(), cid, invoice_id, before["version"]))
            if cur.rowcount != 1:
                raise ToolError("CONFLICT", "发票在保存前被其他操作修改，请重新查询后再确认。")
            self.conn.execute(
                "INSERT INTO operations (op_id, idempotency_key, run_id, session_id, user_id, company_id, invoice_id,"
                " before_json, intended_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (op_id, key, self.scope.run_id, self.scope.session_id, self.scope.user_id, cid, invoice_id,
                 json.dumps(_invoice_view(before), ensure_ascii=False),
                 json.dumps({**changes, "version": before["version"] + 1}, ensure_ascii=False), "pending", _now()))
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        if os.environ.get("FH_FAULT") == "crash_after_commit":
            raise SimulatedCrash(f"{op_id} 已提交但尚未回读")
        # 保存后重新查询核对
        verified, after = verify_operation(self.conn, op_id)
        item_done = False
        if verified and (after["purpose"] or "").strip():
            task = self._task()
            item_done = memory.mark_item_done(self.conn, task["task_id"], invoice_id, f"{op_id} 已回读核验")
        result = {"op_id": op_id, "verified": verified,
                  "before": {k: before[k] for k in changes}, "after": {k: after[k] if after else None for k in changes},
                  "current": _invoice_view(after) if after else None,
                  "open_item_closed": item_done}
        if not verified:
            result["message"] = "写入后回读结果与预期不一致，不能报告保存成功。"
        return result

    def _read_invoice(self, company_id: str, invoice_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?",
                                 (company_id, invoice_id)).fetchone()

    # ---------- 企业切换 ----------

    def switch_company(self, company: str) -> dict:
        key = company.strip()
        row = self.conn.execute("SELECT * FROM companies WHERE company_id=? OR short_name=? OR name=?",
                                (key.upper(), key, key)).fetchone()
        if not row:
            names = "、".join(f'{r["company_id"]} {r["short_name"]}' for r in self.conn.execute("SELECT * FROM companies"))
            raise ToolError("NOT_FOUND", f"没有找到企业“{company}”。可选：{names}。")
        level = self.conn.execute("SELECT level FROM permissions WHERE user_id=? AND company_id=?",
                                  (self.scope.user_id, row["company_id"])).fetchone()
        level = level["level"] if level else "none"
        if level == "none":
            raise ToolError("FORBIDDEN", f"用户 {self.scope.user_id} 无权访问 {row['short_name']}（{row['company_id']}），"
                                         "当前企业保持不变。")
        old = self.scope.company_id
        self.scope.company_id = row["company_id"]
        self.conn.execute("UPDATE sessions SET company_id=? WHERE session_id=?", (row["company_id"], self.scope.session_id))
        self.conn.execute("INSERT INTO messages (session_id, company_id, role, content, run_id, created_at) "
                          "VALUES (?,?,?,?,?,?)", (self.scope.session_id, row["company_id"], "event",
                                                   f"当前企业由 {old} 切换为 {row['company_id']}", self.scope.run_id,
                                                   _now()))
        task = memory.open_task(self.conn, self.scope.user_id, row["company_id"])
        return {"company_id": row["company_id"], "name": row["name"], "permission": LEVEL_TEXT[level],
                "open_task": _task_view(self.conn, task) if task else None,
                "note": "后续查询和修改都限定在该企业；不要沿用上一家企业的发票、月份或待办。"}

    # ---------- 任务记忆 ----------

    def update_task(self, goal: str | None = None, month: str | None = None, note: str | None = None) -> dict:
        self._require("read")
        if month:
            self._check_month(month)
        task = self._task()
        memory.update_task(self.conn, task["task_id"], goal=goal, month=month, note=note)
        return _task_view(self.conn, memory.open_task(self.conn, self.scope.user_id, self.scope.company_id))

    def complete_task(self, summary: str, abandon_pending: bool = False) -> dict:
        task = memory.open_task(self.conn, self.scope.user_id, self.scope.company_id)
        if not task:
            return {"message": "当前企业没有未完成任务。"}
        pending = [r["invoice_id"] for r in memory.items(self.conn, task["task_id"]) if r["state"] == "pending"]
        if pending and not abandon_pending:
            raise ToolError("PENDING_ITEMS", f"还有未处理事项：{'、'.join(pending)}。如用户明确不再处理，"
                                             "设置 abandon_pending=true。", pending=pending)
        memory.update_task(self.conn, task["task_id"], note=f"结束：{summary}")
        memory.close_task(self.conn, task["task_id"])
        return {"closed": task["task_id"], "abandoned": pending}

    # ---------- 长期偏好 ----------

    def remember_preference(self, content: str, user_quote: str, all_companies: bool = False) -> dict:
        if not user_quote or _norm(user_quote) not in _norm("".join(self._session_user_texts())):
            raise ToolError("NOT_AUTHORIZED", "只有用户在本会话中明确要求记住的内容才能保存为长期偏好，"
                                              "user_quote 须是用户原话。")
        mid = memory.add_preference(self.conn, self.scope.user_id, None if all_companies else self.scope.company_id,
                                    content.strip())
        return {"memory_id": mid, "scope": "该用户的所有企业" if all_companies else f"仅 {self.scope.company_id}"}

    def list_memories(self) -> dict:
        prefs = memory.preferences(self.conn, self.scope.user_id, self.scope.company_id)
        task = memory.open_task(self.conn, self.scope.user_id, self.scope.company_id)
        return {"preferences": [_pref_view(p) for p in prefs], "open_task": _task_view(self.conn, task) if task else None}

    def update_memory(self, memory_id: str, content: str) -> dict:
        if not memory.update_preference(self.conn, memory_id, self.scope.user_id, self.scope.company_id, content.strip()):
            raise ToolError("NOT_FOUND", f"当前用户和企业下没有记忆 {memory_id}。")
        return {"memory_id": memory_id, "content": content.strip()}

    def forget_memory(self, memory_id: str) -> dict:
        row = self.conn.execute("SELECT content FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if not memory.delete_preference(self.conn, memory_id, self.scope.user_id, self.scope.company_id):
            raise ToolError("NOT_FOUND", f"当前用户和企业下没有记忆 {memory_id}。")
        _scrub_runs(self.conn, row["content"], f"[已删除的记忆 {memory_id}]")
        return {"memory_id": memory_id, "deleted": True}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def purpose_status(conn: sqlite3.Connection, company_id: str, month: str) -> dict:
    """按适用的有效用途规则检查某月发票（纯计算，不改任务记忆）。工具与演示页概览共用。"""
    if month not in PURPOSE_CHECK_MONTHS:
        raise ToolError("OUT_OF_SCOPE", f"《业务规则》说明初赛只检查 {'、'.join(PURPOSE_CHECK_MONTHS)} 的发票用途，"
                                        f"{month} 的数据只用于金额查询。")
    start, end = _month_bounds(month)
    res = knowledge.resolve(conn, company_id, as_of=date.fromisoformat(end), topic="purpose")
    resolved = res[0] if res else None
    if not resolved or not resolved.chosen:
        raise ToolError("NO_RULE", f"{company_id} 在 {month} 没有有效的发票用途规则，无法判断哪些必填。",
                        excluded_versions=resolved.excluded if resolved else [])
    params = json.loads(resolved.chosen["params"])
    if "purpose_required_min_cents" not in params:
        raise ToolError("RULE_NOT_EXECUTABLE", "适用规则缺少金额门槛参数，请用 rules update --param 补充。")
    threshold = params["purpose_required_min_cents"]
    rows = conn.execute("SELECT * FROM invoices WHERE company_id=? AND issue_date BETWEEN ? AND ? "
                        "ORDER BY invoice_id", (company_id, start, end)).fetchall()
    missing, optional_blank, filled = [], [], []
    for r in rows:
        if (r["purpose"] or "").strip():
            filled.append(r["invoice_id"])
        elif abs(r["amount_cents"]) >= threshold:  # 红字发票按票面金额绝对值比较门槛
            missing.append(r)
        else:
            optional_blank.append(r)
    return {"company_id": company_id, "month": month, "rule": resolved.ref(),
            "requirement": "每张发票都必须填写用途" if threshold == 0
            else f"金额大于或等于 {fmt(threshold)} 元的发票必须填写用途",
            "excluded_versions": resolved.excluded,
            "missing_required": [_invoice_view(r) for r in missing],
            "missing_required_total": fmt(sum(r["amount_cents"] for r in missing)),
            "blank_but_not_required": [r["invoice_id"] for r in optional_blank],
            "filled_count": len(filled), "invoice_count": len(rows), "filled": filled}


def verify_operation(conn: sqlite3.Connection, op_id: str) -> tuple[bool, sqlite3.Row | None]:
    """按 intended_json 回读目标发票并更新操作状态。写入流程和启动恢复共用。"""
    op = conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
    intended = json.loads(op["intended_json"])
    after = conn.execute("SELECT * FROM invoices WHERE company_id=? AND invoice_id=?",
                         (op["company_id"], op["invoice_id"])).fetchone()
    verified = after is not None and all((after[k] or "") == v for k, v in intended.items())
    conn.execute("UPDATE operations SET after_json=?, status=? WHERE op_id=?",
                 (json.dumps(_invoice_view(after), ensure_ascii=False) if after else None,
                  "verified" if verified else "mismatch", op_id))
    return verified, after


def recover_pending(conn: sqlite3.Connection) -> list[dict]:
    """启动时核对中断留下的 pending 写入：回读后标记 verified 或 mismatch，并写入对应任务记忆，
    下一轮对话会如实告诉用户。只做核对，不重新提交。"""
    conn.execute("UPDATE runs SET status='interrupted', ended_at=? WHERE status='running'", (_now(),))
    out = []
    for op in conn.execute("SELECT * FROM operations WHERE status='pending' ORDER BY created_at").fetchall():
        verified, after = verify_operation(conn, op["op_id"])
        conn.execute("UPDATE operations SET recovered_at=? WHERE op_id=?", (_now(), op["op_id"]))
        task = memory.open_task(conn, op["user_id"], op["company_id"])
        if task:
            if verified and after and (after["purpose"] or "").strip():
                memory.mark_item_done(conn, task["task_id"], op["invoice_id"], f"{op['op_id']} 恢复后回读核验")
            memory.update_task(conn, task["task_id"], note=(
                f"上次保存 {op['invoice_id']} 时程序中断，重启后已回读核验：" + ("保存成功" if verified else "内容与预期不一致，未保存成功")
                + f"（{op['op_id']}）"))
        out.append({"op_id": op["op_id"], "invoice": f"{op['company_id']}/{op['invoice_id']}", "verified": verified})
    return out


def _scrub_runs(conn: sqlite3.Connection, text: str | None, placeholder: str) -> None:
    """删除记忆后，把运行记录里保存的模型输入中的原文一并替换，避免删除的内容留存。"""
    if not text:
        return
    for needle in {text, json.dumps(text, ensure_ascii=False)[1:-1]}:
        conn.execute("UPDATE run_steps SET payload=replace(payload, ?, ?) WHERE instr(payload, ?) > 0",
                     (needle, placeholder, needle))


def _invoice_view(r: sqlite3.Row) -> dict:
    return {"invoice_id": r["invoice_id"], "date": r["issue_date"], "seller": r["seller"], "item": r["item"],
            "amount": fmt(r["amount_cents"]), "purpose": r["purpose"], "remark": r["remark"],
            "purpose_filled": bool((r["purpose"] or "").strip())}


def _pref_view(p: sqlite3.Row) -> dict:
    return {"memory_id": p["memory_id"], "content": p["content"],
            "scope": "所有企业" if p["company_id"] is None else p["company_id"], "revision": p["revision"]}


def _task_view(conn: sqlite3.Connection, task: sqlite3.Row) -> dict:
    its = memory.items(conn, task["task_id"])
    return {"task_id": task["task_id"], "goal": task["goal"], "month": task["month"],
            "pending": [r["invoice_id"] for r in its if r["state"] == "pending"],
            "done": [r["invoice_id"] for r in its if r["state"] == "done"],
            "notes": json.loads(task["notes"]), "updated_at": task["updated_at"]}


# ---------- 工具定义（OpenAI 兼容 function calling） ----------

def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required, "additionalProperties": False}}}


MONTH = {"type": "string", "description": "YYYY-MM，必须是用户说过或任务记忆中的月份"}
TOOL_SPECS = [
    _fn("summarize_month", "由程序计算当前企业某月发票总额、银行收入、银行支出（分别统计）。查询金额时使用。",
        {"month": MONTH}, ["month"]),
    _fn("query_invoices", "查询当前企业某月发票明细，可按发票编号筛选或只看未填用途的。",
        {"month": MONTH, "invoice_ids": {"type": "array", "items": {"type": "string"}},
         "missing_purpose_only": {"type": "boolean"}}, ["month"]),
    _fn("query_bank", "查询当前企业某月银行流水明细（只读）。",
        {"month": MONTH, "direction": {"type": "string", "enum": ["income", "expense"]}}, ["month"]),
    _fn("check_purpose", "按当前企业适用的有效用途规则，检查某月哪些发票必须填用途但未填，并更新任务待办。",
        {"month": MONTH}, ["month"]),
    _fn("search_rules", "在当前企业适用的有效规则中查找条款原文。month 指业务月份，用于选择当时有效的版本。",
        {"query": {"type": "string"}, "month": MONTH}, ["query"]),
    _fn("get_rule", "读取某条规则当前适用版本的全文。",
        {"rule_id": {"type": "string", "description": "如 R002"}, "month": MONTH}, ["rule_id"]),
    _fn("update_invoice",
        "修改当前企业一张发票的用途和/或备注，保存后程序会回读核对。仅在用户本次会话明确要求修改时调用；"
        "内容必须使用用户给出的原文。",
        {"invoice_id": {"type": "string"},
         "purpose": {"type": "string", "description": "用户给出的用途原文"},
         "remark": {"type": "string", "description": "用户给出的备注原文"},
         "user_quote": {"type": "string", "description": "用户要求这次修改的原话（逐字摘录）"}},
        ["invoice_id", "user_quote"]),
    _fn("switch_company", "用户要求处理另一家企业时切换当前企业，程序会检查权限并载入该企业的任务。",
        {"company": {"type": "string", "description": "企业编号、简称或全称"}}, ["company"]),
    _fn("update_task", "更新当前企业的任务记忆：目标、关注月份或一条已确认的事实。",
        {"goal": {"type": "string"}, "month": MONTH, "note": {"type": "string"}}, []),
    _fn("complete_task", "当前企业的任务全部完成或用户明确不再处理时结束任务。",
        {"summary": {"type": "string"}, "abandon_pending": {"type": "boolean"}}, ["summary"]),
    _fn("remember_preference", "保存用户明确要求记住的长期偏好（如回答格式、称呼）。默认只对当前企业有效。",
        {"content": {"type": "string"}, "user_quote": {"type": "string", "description": "用户要求记住的原话"},
         "all_companies": {"type": "boolean", "description": "仅当用户明确说对所有企业都适用时为 true"}},
        ["content", "user_quote"]),
    _fn("list_memories", "列出当前用户在当前企业可用的长期偏好和任务记忆。", {}, []),
    _fn("update_memory", "按用户要求更正一条长期偏好。",
        {"memory_id": {"type": "string"}, "content": {"type": "string"}}, ["memory_id", "content"]),
    _fn("forget_memory", "按用户要求删除一条长期偏好。", {"memory_id": {"type": "string"}}, ["memory_id"]),
]
MODEL_RULES_SPECS = [s for s in TOOL_SPECS if s["function"]["name"] not in ("check_purpose", "search_rules", "get_rule")] + [
    _fn("list_rule_documents", "返回与当前企业相关的全部规则文档（各版本原文与状态），由你自行判断适用哪一版。", {}, [])]


def tool_specs(variant: str = "baseline") -> list[dict]:
    return MODEL_RULES_SPECS if variant == "model_rules" else TOOL_SPECS


TOOL_NAMES = {t["function"]["name"] for t in TOOL_SPECS + MODEL_RULES_SPECS}


_JSON_TYPES = {"string": str, "boolean": bool, "array": list}


def _check_args(spec: dict, args: dict) -> list[str]:
    """按工具声明的 JSON Schema 检查参数：多余字段、缺少必填、类型与枚举。"""
    params = spec["function"]["parameters"]
    props, problems = params["properties"], []
    for k in params.get("required", []):
        if args.get(k) is None:
            problems.append(f"缺少必填参数 {k}")
    for k, v in args.items():
        if k not in props:
            problems.append(f"没有参数 {k}")
            continue
        p = props[k]
        if v is None or isinstance(v, _JSON_TYPES[p["type"]]):
            if p["type"] == "array" and v is not None and not all(isinstance(x, str) for x in v):
                problems.append(f"{k} 必须是字符串数组")
            if "enum" in p and v is not None and v not in p["enum"]:
                problems.append(f"{k} 只能是 {p['enum']} 之一")
        else:
            problems.append(f"{k} 应为 {p['type']}，实际是 {type(v).__name__}")
    return problems


def dispatch(box: Toolbox, name: str, raw_args: str | dict, variant: str = "baseline") -> dict:
    """执行一次工具调用，统一返回 {ok, data | error}。参数错误和守卫拒绝都作为结果交还模型。"""
    specs = {s["function"]["name"]: s for s in tool_specs(variant)}
    if name not in specs:
        return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": f"没有工具 {name}"}}
    try:
        args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else dict(raw_args)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"ok": False, "error": {"code": "BAD_JSON", "message": "参数不是合法 JSON 对象"}}
    if not isinstance(args, dict):
        return {"ok": False, "error": {"code": "BAD_JSON", "message": "参数必须是 JSON 对象"}}
    if problems := _check_args(specs[name], args):
        return {"ok": False, "error": {"code": "BAD_ARG", "message": "；".join(problems)}}
    args = {k: v for k, v in args.items() if v is not None}  # 可选参数传 null 视同未传
    try:
        return {"ok": True, "data": getattr(box, name)(**args)}
    except ToolError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message, **e.extra}}
    except TypeError as e:
        return {"ok": False, "error": {"code": "BAD_ARG", "message": str(e)}}
