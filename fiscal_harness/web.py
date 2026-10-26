"""演示网页的 HTTP 接口。页面只展示后端真实结果：对话、当前数据、任务与记忆、规则、运行记录。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, harness, knowledge, memory, runlog
from .llm import LLMError, from_settings
from .money import fmt, to_cents
from .tools import LEVEL_TEXT, ToolError, _pref_view, _task_view, purpose_status, recover_pending

STATIC = Path(__file__).resolve().parent.parent / "web" / "index.html"


class NewSession(BaseModel):
    user_id: str
    company_id: str


class Message(BaseModel):
    text: str


class RuleVersion(BaseModel):
    rule_id: str
    version: str
    body: str
    effective_from: str
    name: str | None = None
    companies: list[str] | None = None
    purpose_required_min: str | None = None  # 元；空表示从正文解析


class RuleStatus(BaseModel):
    status: str


class MemoryEdit(BaseModel):
    user_id: str
    company_id: str
    content: str | None = None


def create_app(settings) -> FastAPI:
    app = FastAPI(title="财税 Agent Harness")
    llm = from_settings(settings)
    recovered = recover_pending(db.connect(settings.db_path))  # 启动时核对中断留下的写入

    def conn():
        return db.connect(settings.db_path)

    @app.get("/")
    def index():
        return FileResponse(STATIC)

    @app.get("/api/meta")
    def meta():
        c = conn()
        users = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY user_id")]
        companies = [dict(r) for r in c.execute("SELECT * FROM companies ORDER BY company_id")]
        perms = {f"{r['user_id']}/{r['company_id']}": LEVEL_TEXT[r["level"]] for r in c.execute("SELECT * FROM permissions")}
        return {"users": users, "companies": companies, "permissions": perms, "model": llm.model,
                "today": settings.today.isoformat(), "recovered_on_start": recovered}

    @app.post("/api/sessions")
    def new_session(body: NewSession):
        try:
            sid = harness.start_session(conn(), body.user_id, body.company_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"session_id": sid}

    @app.get("/api/sessions/{sid}")
    def session(sid: str):
        c = conn()
        row = c.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "没有该会话")
        msgs = [dict(r) for r in c.execute("SELECT role, content, run_id, company_id FROM messages "
                                           "WHERE session_id=? ORDER BY id", (sid,))]
        return {**dict(row), "messages": msgs}

    @app.post("/api/sessions/{sid}/messages")
    def send(sid: str, body: Message):
        if not body.text.strip():
            raise HTTPException(400, "消息为空")
        try:
            res = harness.run_turn(conn(), llm, settings, sid, body.text.strip())
        except ValueError as e:
            raise HTTPException(404, str(e))
        except LLMError as e:
            raise HTTPException(502, str(e))
        return res.__dict__

    @app.get("/api/runs")
    def runs(session_id: str | None = None, limit: int = 30):
        c = conn()
        sql, args = "SELECT run_id, session_id, user_id, company_id, user_message, status, started_at FROM runs", []
        if session_id:
            sql, args = sql + " WHERE session_id=?", [session_id]
        return [dict(r) for r in c.execute(sql + " ORDER BY started_at DESC LIMIT ?", args + [limit])]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        r = runlog.load(conn(), run_id)
        if not r:
            raise HTTPException(404, "没有该运行记录")
        return r

    @app.get("/api/invoices")
    def invoices(company_id: str, month: str | None = None):
        c = conn()
        sql, args = "SELECT * FROM invoices WHERE company_id=?", [company_id]
        if month:
            sql, args = sql + " AND substr(issue_date,1,7)=?", args + [month]
        return [{**{k: r[k] for k in ("invoice_id", "issue_date", "seller", "item", "purpose", "remark", "version",
                                      "updated_at")}, "amount": fmt(r["amount_cents"])}
                for r in c.execute(sql + " ORDER BY issue_date, invoice_id", args)]

    @app.get("/api/operations")
    def operations(company_id: str | None = None):
        c = conn()
        sql, args = "SELECT * FROM operations", []
        if company_id:
            sql, args = sql + " WHERE company_id=?", [company_id]
        return [{**dict(r), "before_json": json.loads(r["before_json"]),
                 "after_json": json.loads(r["after_json"]) if r["after_json"] else None}
                for r in c.execute(sql + " ORDER BY created_at DESC", args)]

    @app.get("/api/memory")
    def memories(user_id: str, company_id: str):
        c = conn()
        task = memory.open_task(c, user_id, company_id)
        return {"preferences": [_pref_view(p) for p in memory.preferences(c, user_id, company_id)],
                "task": _task_view(c, task) if task else None}

    @app.patch("/api/memory/{memory_id}")
    def edit_memory(memory_id: str, body: MemoryEdit):
        if not body.content or not memory.update_preference(conn(), memory_id, body.user_id, body.company_id,
                                                            body.content.strip()):
            raise HTTPException(404, "找不到该记忆")
        return {"ok": True}

    @app.delete("/api/memory/{memory_id}")
    def delete_memory(memory_id: str, user_id: str, company_id: str):
        if not memory.delete_preference(conn(), memory_id, user_id, company_id):
            raise HTTPException(404, "找不到该记忆")
        return {"ok": True}

    @app.get("/api/overview")
    def overview(user_id: str, company_id: str):
        """演示页首屏：程序直接计算的月度汇总与 9 月用途检查（不经模型、不改任务记忆）。"""
        c = conn()
        level = c.execute("SELECT level FROM permissions WHERE user_id=? AND company_id=?", (user_id, company_id)).fetchone()
        if not level or level["level"] == "none":
            return {"forbidden": True}
        months = []
        for month in ("2026-07", "2026-08", "2026-09"):
            inv = c.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s FROM invoices WHERE company_id=? "
                            "AND substr(issue_date,1,7)=?", (company_id, month)).fetchone()
            bank = {r["direction"]: r["s"] for r in c.execute(
                "SELECT direction, SUM(amount_cents) s FROM bank_txns WHERE company_id=? AND substr(txn_date,1,7)=? "
                "GROUP BY direction", (company_id, month))}
            months.append({"month": month, "invoice_count": inv["n"], "invoice_total": fmt(inv["s"]),
                           "bank_income": fmt(bank.get("income", 0)), "bank_expense": fmt(bank.get("expense", 0))})
        try:
            status = purpose_status(c, company_id, "2026-09")
            status.pop("filled")
        except ToolError as e:
            status = {"error": e.message}
        return {"forbidden": False, "months": months, "purpose_2026_09": status}

    @app.post("/api/rules")
    def add_rule(body: RuleVersion):
        params = None
        if body.purpose_required_min not in (None, ""):
            params = {"purpose_required_min_cents": to_cents(body.purpose_required_min)}
        try:
            rule = knowledge.add_version(conn(), body.rule_id.strip().upper(), body.version.strip(), body.body.strip(),
                                         name=body.name or None, companies=body.companies or None,
                                         effective_from=body.effective_from, params=params)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return rule

    @app.post("/api/rules/{rule_id}/{version}/status")
    def rule_status(rule_id: str, version: str, body: RuleStatus):
        if body.status not in ("active", "disabled"):
            raise HTTPException(400, "status 只能是 active 或 disabled")
        try:
            knowledge.set_status(conn(), rule_id, version, body.status)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.get("/api/rules")
    def rules(company_id: str):
        c = conn()
        resolved = {(r.chosen["rule_id"], r.chosen["version"])
                    for r in knowledge.resolve(c, company_id, as_of=settings.today) if r.chosen}
        out = []
        for r in c.execute("SELECT * FROM rule_versions ORDER BY rule_id, effective_from"):
            if company_id not in json.loads(r["companies"]):
                continue
            out.append({"rule_id": r["rule_id"], "version": r["version"], "name": r["name"], "status": r["status"],
                        "effective_from": r["effective_from"], "body": r["body"].split("\n"),
                        "params": json.loads(r["params"]),
                        "applicable_today": (r["rule_id"], r["version"]) in resolved})
        return out

    return app
