"""演示网页的 HTTP 接口。页面只展示后端真实结果：对话、当前数据、任务与记忆、规则、运行记录。"""
from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, harness, invoice_files, knowledge, memory, ocr, runlog
from .llm import LLMError, from_settings
from .money import fmt, to_cents
from .tools import LEVEL_TEXT, ToolError, _pref_view, _task_view, purpose_status, recover_pending

STATIC = Path(__file__).resolve().parent.parent / "web" / "index.html"


class NewSession(BaseModel):
    user_id: str
    company_id: str


class UploadedFile(BaseModel):
    name: str
    data: str  # base64


class Message(BaseModel):
    text: str = ""
    files: list[UploadedFile] = []  # 随消息上传的发票：程序解析校验后暂存，导入由对话中用户明确要求


class RuleVersion(BaseModel):
    session_id: str
    rule_id: str
    version: str
    body: str
    effective_from: str
    name: str | None = None
    companies: list[str] | None = None
    purpose_required_min: str | None = None  # 元；空表示从正文解析


class RuleStatus(BaseModel):
    session_id: str
    status: str


class InvoiceUpload(BaseModel):
    session_id: str  # 导入人与企业取自会话；导入成功后在该会话记一条系统事件
    files: list[UploadedFile]
    commit: bool = False
    accept_buyer_mismatch: bool = False
    allow_test_samples: bool = False
    confirm_ocr: bool = False  # 识别件：操作员已对照票面核对号码和日期


class MemoryEdit(BaseModel):
    session_id: str
    content: str | None = None


def create_app(settings) -> FastAPI:
    app = FastAPI(title="财税 Agent Harness")
    llm = from_settings(settings)
    recovered = recover_pending(db.connect(settings.db_path))  # 启动时核对中断留下的写入
    recognizer = ocr.from_settings(settings)  # 扫描件/图片识别，默认关闭

    def conn():
        return db.connect(settings.db_path)

    def viewer(session_id: str, need: str | None = "read"):
        """数据接口的身份与企业一律取自会话，不信任请求里自报的用户或企业；权限口径与对话工具相同。"""
        c = conn()
        row = c.execute("SELECT user_id, company_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(404, "没有该会话")
        lv = c.execute("SELECT level FROM permissions WHERE user_id=? AND company_id=?",
                       (row["user_id"], row["company_id"])).fetchone()
        lv = lv["level"] if lv else "none"
        if need and (lv == "none" or (need == "write" and lv != "write")):
            raise HTTPException(403, f"{row['user_id']} 对 {row['company_id']} 的权限是“{LEVEL_TEXT[lv]}”，"
                                     f"不能{'修改' if need == 'write' else '查看'}")
        return c, row["user_id"], row["company_id"]

    def require_rule_write(c, user_id: str, companies: list[str]) -> None:
        """规则可能由多家企业共用：改动前逐一核对用户对每家受影响企业都有修改权限，不只看当前会话企业。"""
        if not companies:
            raise HTTPException(400, "规则必须指定适用企业")
        lacking = []
        for cid in companies:
            lv = c.execute("SELECT level FROM permissions WHERE user_id=? AND company_id=?", (user_id, cid)).fetchone()
            if not lv or lv["level"] != "write":
                lacking.append(cid)
        if lacking:
            raise HTTPException(403, f"{user_id} 对 {'、'.join(lacking)} 没有修改权限，不能更改适用于这些企业的规则")

    @app.get("/")
    def index():
        return FileResponse(STATIC)

    @app.get("/ocr-review")
    def ocr_review():
        return FileResponse(STATIC.parent / "ocr_review.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/meta")
    def meta():
        c = conn()
        users = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY user_id")]
        companies = [dict(r) for r in c.execute("SELECT * FROM companies ORDER BY company_id")]
        perms = {f"{r['user_id']}/{r['company_id']}": LEVEL_TEXT[r["level"]] for r in c.execute("SELECT * FROM permissions")}
        return {"users": users, "companies": companies, "permissions": perms, "model": llm.model,
                "today": settings.today.isoformat(), "recovered_on_start": recovered,
                "allow_foreign_buyer": settings.allow_foreign_buyer, "allow_test_samples": settings.allow_test_samples,
                "ocr": recognizer is not None}

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

    def parse_uploads(files: list[UploadedFile]) -> list[invoice_files.ParsedInvoice]:
        parsed = []
        for f in files:
            try:
                data = base64.b64decode(f.data, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(400, f"{f.name} 不是有效的 base64 内容")
            parsed.append(invoice_files.parse_bytes(Path(f.name).name, data, recognizer))
        return parsed

    @app.post("/api/sessions/{sid}/messages")
    def send(sid: str, body: Message):
        parsed = []
        if body.files:
            viewer(sid)  # 上传至少要能查看当前企业；导入另需修改权限，由导入工具把关
            if len(body.files) > 10:
                raise HTTPException(400, "一条消息最多附带 10 个文件")
            parsed = parse_uploads(body.files)
        text = body.text.strip() or (f"上传了 {len(parsed)} 个发票文件" if parsed else "")
        if not text:
            raise HTTPException(400, "消息为空")
        try:
            res = harness.run_turn(conn(), llm, settings, sid, text, attachments=parsed)
        except ValueError as e:
            raise HTTPException(404, str(e))
        except LLMError as e:
            raise HTTPException(502, str(e))
        return res.__dict__

    @app.get("/api/runs")
    def runs(session_id: str, limit: int = 30):
        c, _, _ = viewer(session_id, need=None)  # 只列本会话的运行记录
        return [dict(r) for r in c.execute("SELECT run_id, session_id, user_id, company_id, user_message, status, "
                                           "started_at FROM runs WHERE session_id=? ORDER BY started_at DESC LIMIT ?",
                                           (session_id, limit))]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, session_id: str):
        c, _, _ = viewer(session_id, need=None)
        r = runlog.load(c, run_id)
        if not r or r["session_id"] != session_id:
            raise HTTPException(404, "本会话没有该运行记录")
        return r

    @app.get("/api/invoices")
    def invoices(session_id: str, month: str | None = None):
        c, _, company_id = viewer(session_id)
        sql, args = "SELECT * FROM invoices WHERE company_id=?", [company_id]
        if month:
            sql, args = sql + " AND substr(issue_date,1,7)=?", args + [month]
        return [{**{k: r[k] for k in ("invoice_id", "issue_date", "seller", "item", "purpose", "remark", "version",
                                      "updated_at")}, "amount": fmt(r["amount_cents"])}
                for r in c.execute(sql + " ORDER BY issue_date, invoice_id", args)]

    @app.get("/api/invoice-months")
    def invoice_months(session_id: str):
        c, _, company_id = viewer(session_id)
        return [r["m"] for r in c.execute("SELECT DISTINCT substr(issue_date,1,7) m FROM invoices "
                                               "WHERE company_id=? ORDER BY m", (company_id,))]

    @app.post("/api/invoice-files")
    def upload_invoices(body: InvoiceUpload):
        """预览（commit=false）或导入发票文件。解析与校验全部由程序完成，不经模型。"""
        c, user_id, company_id = viewer(body.session_id, need="write")
        if body.accept_buyer_mismatch and not settings.allow_foreign_buyer:
            raise HTTPException(403, "未开启 FH_ALLOW_FOREIGN_BUYER，不能导入购买方不是本企业的发票")
        if body.allow_test_samples and not settings.allow_test_samples:
            raise HTTPException(403, "未开启 FH_ALLOW_TEST_SAMPLES，不能导入测试样张")
        if not body.files or len(body.files) > 50:
            raise HTTPException(400, "一次上传 1–50 个文件")
        parsed = parse_uploads(body.files)
        results = invoice_files.import_files(c, company_id, parsed, commit=body.commit,
                                             accept_buyer_mismatch=body.accept_buyer_mismatch,
                                             allow_test_samples=body.allow_test_samples, confirm_ocr=body.confirm_ocr)
        event = None
        if body.commit:
            event = invoice_files.record_import_event(c, body.session_id, company_id, results, parsed)
        return {"event": event, "results": [{**r, "parsed": p.view()} for r, p in zip(results, parsed)]}

    @app.get("/api/operations")
    def operations(session_id: str):
        c, _, company_id = viewer(session_id)
        sql, args = "SELECT * FROM operations WHERE company_id=?", [company_id]
        return [{**dict(r), "before_json": json.loads(r["before_json"]),
                 "after_json": json.loads(r["after_json"]) if r["after_json"] else None}
                for r in c.execute(sql + " ORDER BY created_at DESC", args)]

    @app.get("/api/memory")
    def memories(session_id: str):
        c, user_id, company_id = viewer(session_id, need=None)  # 用户自己的记忆
        task = memory.open_task(c, user_id, company_id)
        return {"preferences": [_pref_view(p) for p in memory.preferences(c, user_id, company_id)],
                "task": _task_view(c, task) if task else None}

    @app.patch("/api/memory/{memory_id}")
    def edit_memory(memory_id: str, body: MemoryEdit):
        c, user_id, company_id = viewer(body.session_id, need=None)
        if not body.content or not memory.update_preference(c, memory_id, user_id, company_id, body.content.strip()):
            raise HTTPException(404, "找不到该记忆")
        return {"ok": True}

    @app.delete("/api/memory/{memory_id}")
    def delete_memory(memory_id: str, session_id: str):
        c, user_id, company_id = viewer(session_id, need=None)
        if not memory.delete_preference(c, memory_id, user_id, company_id):
            raise HTTPException(404, "找不到该记忆")
        return {"ok": True}

    @app.get("/api/overview")
    def overview(session_id: str):
        """演示页首屏：程序直接计算的月度汇总与 9 月用途检查（不经模型、不改任务记忆）。"""
        try:
            c, _, company_id = viewer(session_id)
        except HTTPException as e:
            if e.status_code == 403:
                return {"forbidden": True}
            raise
        months = []
        have = [r["m"] for r in c.execute("SELECT DISTINCT substr(issue_date,1,7) m FROM invoices WHERE company_id=?",
                                          (company_id,))]
        for month in sorted({"2026-07", "2026-08", "2026-09", *have}):  # 官方三个月 + 导入发票所在月份
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
        c, user_id, _ = viewer(body.session_id, need="write")
        rule_id = body.rule_id.strip().upper()
        prev = c.execute("SELECT companies FROM rule_versions WHERE rule_id=? ORDER BY effective_from DESC, created_at DESC",
                         (rule_id,)).fetchone()
        # 新版本影响的企业：本次指定的，以及该规则此前适用的（新版本会取代它们的旧版本）
        targets = set(body.companies or []) | set(json.loads(prev["companies"]) if prev else [])
        require_rule_write(c, user_id, sorted(targets))
        params = None
        if body.purpose_required_min not in (None, ""):
            params = {"purpose_required_min_cents": to_cents(body.purpose_required_min)}
        try:
            rule = knowledge.add_version(c, rule_id, body.version.strip(), body.body.strip(),
                                         name=body.name or None, companies=body.companies or None,
                                         effective_from=body.effective_from, params=params)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return rule

    @app.post("/api/rules/{rule_id}/{version}/status")
    def rule_status(rule_id: str, version: str, body: RuleStatus):
        c, user_id, _ = viewer(body.session_id, need="write")
        if body.status not in ("active", "disabled"):
            raise HTTPException(400, "status 只能是 active 或 disabled")
        row = c.execute("SELECT companies FROM rule_versions WHERE rule_id=? AND version=?", (rule_id, version)).fetchone()
        if not row:
            raise HTTPException(404, f"找不到规则 {rule_id} {version} 版")
        require_rule_write(c, user_id, json.loads(row["companies"]))
        try:
            knowledge.set_status(c, rule_id, version, body.status)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.get("/api/rules")
    def rules(session_id: str):
        c, _, company_id = viewer(session_id)
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
