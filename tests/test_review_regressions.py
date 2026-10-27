"""v0.2.0 外部评审（docs/reviews）提出的问题：每条复现路径都转成回归测试。模型一律用预设脚本，不调用真实接口。"""
import dataclasses
import json

import pytest
from fastapi import HTTPException

from fiscal_harness import db, harness, importer, runlog, web
from fiscal_harness.llm import ScriptedLLM, tool_call
from fiscal_harness.tools import Scope, ToolError, Toolbox, dispatch

from .conftest import TODAY, say


def fresh(conn, text, user="U001", company="C001"):
    sid = harness.start_session(conn, user, company)
    say(conn, sid, company, text)
    return sid, Toolbox(conn, Scope(sid, user, company, TODAY))


def purpose(conn, company, invoice):
    return conn.execute("SELECT purpose FROM invoices WHERE company_id=? AND invoice_id=?", (company, invoice)).fetchone()[0]


# ---------- 1. 授权绑定企业、发票、内容与来源原话 ----------

def test_cannot_write_an_invoice_the_user_did_not_name(conn):
    text = "请把甲企业9月F003用途改为寄送合同快递并保存"
    _, box = fresh(conn, text)
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", user_quote=text, purpose="寄送合同快递")
    assert e.value.code == "INVOICE_NOT_REQUESTED" and purpose(conn, "C001", "F002") is None
    assert box.update_invoice("F003", user_quote=text, purpose="寄送合同快递")["verified"]


def test_explicit_no_save_is_not_authorization(conn):
    text = "9月F002用途是打印复印纸，只是讨论，先别保存"
    _, box = fresh(conn, text)
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", user_quote=text, purpose="打印复印纸")
    assert e.value.code == "NOT_AUTHORIZED" and purpose(conn, "C001", "F002") is None


def test_follow_up_save_in_same_session_uses_the_named_invoice(conn):
    sid, box = fresh(conn, "9月F004的用途是客户接待住宿，先别保存，我再想想")
    say(conn, sid, "C001", "可以了，保存吧")
    r = box.update_invoice("F004", user_quote="可以了，保存吧", purpose="客户接待住宿")
    assert r["verified"] and purpose(conn, "C001", "F004") == "客户接待住宿"
    with pytest.raises(ToolError) as e:  # “保存吧”指的是 F004，不能拿来改别的票
        box.update_invoice("F002", user_quote="可以了，保存吧", purpose="客户接待住宿")
    assert e.value.code == "INVOICE_NOT_REQUESTED"


def test_quote_from_before_company_switch_is_void(conn):
    text = "请把甲企业9月F002用途改为打印复印纸并保存"
    sid, box = fresh(conn, text)
    box.update_invoice("F002", user_quote=text, purpose="打印复印纸")
    say(conn, sid, "C001", "切到乙企业")
    box.switch_company("C002")
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", user_quote=text, purpose="打印复印纸")
    assert e.value.code == "NOT_AUTHORIZED" and purpose(conn, "C002", "F002") is None
    with pytest.raises(ToolError) as e:  # 甲企业对话里说过的月份也不带到乙企业
        box.summarize_month("2026-09")
    assert e.value.code == "MONTH_UNCONFIRMED"


def test_switch_message_itself_counts_for_new_company(conn):
    sid, box = fresh(conn, "切到乙企业，看看9月还有哪些必须填的用途没填")
    box.switch_company("C002")
    assert box.summarize_month("2026-09")["month"] == "2026-09"


# ---------- 3. 幂等只拦同一句原话的重试，不拦 A → B → A 的更正 ----------

def test_revert_a_b_a_is_a_new_operation(conn):
    first, second, third = "F002用途写打印复印纸，保存", "F002用途改成办公打印耗材，保存", "还是改回打印复印纸，保存到F002"
    sid, box = fresh(conn, first)
    box.update_invoice("F002", user_quote=first, purpose="打印复印纸")
    retry = box.update_invoice("F002", user_quote=first, purpose="打印复印纸")
    say(conn, sid, "C001", second)
    box.update_invoice("F002", user_quote=second, purpose="办公打印耗材")
    say(conn, sid, "C001", third)
    r = box.update_invoice("F002", user_quote=third, purpose="打印复印纸")
    assert r["verified"] and not r.get("duplicate") and purpose(conn, "C001", "F002") == "打印复印纸"
    assert retry["duplicate"] and retry["current_matches_request"]
    assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 3


# ---------- 4. 同一轮切换企业后重建模型输入 ----------

def test_same_turn_switch_rebuilds_context(conn, settings):
    sid, _ = fresh(conn, "甲企业9月F003专属说明")
    conn.execute("INSERT INTO messages (session_id,company_id,role,content,created_at) VALUES (?,?,?,?,?)",
                 (sid, "C001", "assistant", "甲企业专属历史内容", "2026-10-26T12:00:00"))
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("switch_company", {"company": "C002"}, "c1"),
                        tool_call("query_invoices", {"month": "2026-09"}, "c2")]},
        {"content": "已切换"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "切到乙企业，看看9月发票")
    following = llm.calls[1]
    assert "当前企业：C002" in following[0]["content"] and "当前企业：C001" not in following[0]["content"]
    assert "甲企业专属历史内容" not in json.dumps(following, ensure_ascii=False)
    assert [c["error"] for c in res.tool_calls] == [None, "SKIPPED_AFTER_SWITCH"]
    # 每个 tool_call 都有对应的 tool 结果，协议完整
    ids = {c["id"] for m in following if m.get("tool_calls") for c in m["tool_calls"]}
    assert ids == {m["tool_call_id"] for m in following if m["role"] == "tool"}


# ---------- 5. 脱敏覆盖持久化与导出的全部字段 ----------

def test_secret_redacted_everywhere(conn, settings, monkeypatch):
    fake = "sk" + "-reviewDUMMY1234567890abcdef"
    monkeypatch.setenv("LLM_API_KEY", fake)
    sid = harness.start_session(conn, "U001", "C001")
    res = harness.run_turn(conn, ScriptedLLM([{"content": f"密钥是 {fake}"}]), settings, sid, f"我的key是{fake}")
    dumps = [json.dumps(runlog.load(conn, res.run_id), ensure_ascii=False),
             json.dumps([dict(r) for r in conn.execute("SELECT * FROM runs")], ensure_ascii=False),
             json.dumps([dict(r) for r in conn.execute("SELECT * FROM messages")], ensure_ascii=False),
             json.dumps([dict(r) for r in conn.execute("SELECT * FROM run_steps")], ensure_ascii=False)]
    assert not any(fake in d for d in dumps)


# ---------- 6. 参数类型校验与异常收尾 ----------

def test_malformed_tool_argument_returns_bad_arg(conn, settings):
    sid, _ = fresh(conn, "9月F002用途填打印复印纸并保存")
    llm = ScriptedLLM([{"tool_calls": [tool_call("update_invoice", {"invoice_id": 2, "purpose": "打印复印纸",
                                                                      "user_quote": "F002"})]},
                       {"content": "参数有误，请确认"}])
    res = harness.run_turn(conn, llm, settings, sid, "保存")
    assert res.status == "completed" and res.tool_calls[0]["error"] == "BAD_ARG"
    assert conn.execute("SELECT status FROM runs WHERE run_id=?", (res.run_id,)).fetchone()[0] == "completed"


@pytest.mark.parametrize("args,needle", [
    ({"month": 202609}, "month 应为 string"), ({}, "缺少必填参数 month"), ({"month": "2026-09", "x": 1}, "没有参数 x"),
    ({"month": "2026-09", "invoice_ids": ["F001", 2]}, "字符串数组"), ("[1]", "JSON 对象"),
])
def test_schema_validation(box_for, args, needle):
    r = dispatch(box_for(), "query_invoices", json.dumps(args) if isinstance(args, dict) else args)
    assert not r["ok"] and needle in r["error"]["message"]


def test_null_optional_argument_is_ignored(box_for):
    r = dispatch(box_for(), "query_bank", {"month": "2026-09", "direction": None})
    assert r["ok"]


def test_unexpected_exception_closes_the_run(conn, settings, monkeypatch):
    monkeypatch.setattr(Toolbox, "list_memories", lambda self: 1 / 0)
    sid = harness.start_session(conn, "U001", "C001")
    res = harness.run_turn(conn, ScriptedLLM([{"tool_calls": [tool_call("list_memories", {})]}]), settings, sid, "看看")
    assert res.status == "error"
    row = conn.execute("SELECT status, ended_at FROM runs WHERE run_id=?", (res.run_id,)).fetchone()
    assert row["status"] == "error" and row["ended_at"]


# ---------- 2. 网页数据接口按会话身份校验权限 ----------

@pytest.fixture
def app_api(settings, tmp_path, monkeypatch):
    s = dataclasses.replace(settings, db_path=tmp_path / "web.db")
    c = db.connect(s.db_path)
    importer.reset_and_import(c, s.sample_xlsx, s.rules_docx)
    monkeypatch.setattr(web, "from_settings", lambda _: ScriptedLLM([]))
    app = web.create_app(s)
    routes = {(r.path, next(iter(r.methods))): r.endpoint for r in app.routes if hasattr(r, "methods")}
    return lambda path, method="GET": routes[(path, method)], c


def test_web_data_endpoints_enforce_session_permission(app_api):
    api, c = app_api
    sid = api("/api/sessions", "POST")(web.NewSession(user_id="U002", company_id="C002"))["session_id"]
    assert api("/api/overview")(session_id=sid) == {"forbidden": True}
    for path, kw in (("/api/invoices", {"month": "2026-09"}), ("/api/operations", {}), ("/api/invoice-months", {}),
                     ("/api/rules", {})):
        with pytest.raises(HTTPException) as e:
            api(path)(session_id=sid, **kw)
        assert e.value.status_code == 403, path
    # 仅查看的用户能看甲企业数据，但不能导入发票或改规则
    ro = api("/api/sessions", "POST")(web.NewSession(user_id="U002", company_id="C001"))["session_id"]
    assert len(api("/api/invoices")(session_id=ro, month="2026-09")) == 10
    for call in (lambda: api("/api/invoice-files", "POST")(web.InvoiceUpload(session_id=ro, files=[])),
                 lambda: api("/api/rules/{rule_id}/{version}/status", "POST")(
                     "R002", "2.0", web.RuleStatus(session_id=ro, status="disabled"))):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        api("/api/invoices")(session_id="S-nope", month="2026-09")
    assert e.value.status_code == 404


def test_web_runs_are_limited_to_own_session(app_api, settings):
    api, c = app_api
    a = api("/api/sessions", "POST")(web.NewSession(user_id="U001", company_id="C001"))["session_id"]
    b = api("/api/sessions", "POST")(web.NewSession(user_id="U002", company_id="C001"))["session_id"]
    run_id = harness.run_turn(c, ScriptedLLM([{"content": "好"}]), settings, a, "你好").run_id
    assert api("/api/runs/{run_id}")(run_id, session_id=a)["run_id"] == run_id
    with pytest.raises(HTTPException) as e:
        api("/api/runs/{run_id}")(run_id, session_id=b)
    assert e.value.status_code == 404
    assert api("/api/runs")(session_id=b) == []
