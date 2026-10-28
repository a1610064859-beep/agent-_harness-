"""2026-10-28 扩展测试（E:\\claude\\inbox\\fiscal-harness-test-20261028\\测试报告.md）的 12 项问题：
复现路径转成回归测试，另补正常说法不被误拦的用例。模型调用一律用预设参数或预设脚本，不调用真实接口。"""
import json

import pytest
from fastapi import HTTPException

from fiscal_harness import context, harness, web
from fiscal_harness import invoice_files as inf
from fiscal_harness.llm import ScriptedLLM, tool_call
from fiscal_harness.tools import Scope, ToolError, Toolbox

from .conftest import TODAY, say
from .test_invoice_files import KV
from .test_review_regressions import app_api, fresh, purpose  # noqa: F401  （app_api 是夹具）


def rejects(box, code, invoice, quote, **changes):
    with pytest.raises(ToolError) as e:
        box.update_invoice(invoice, user_quote=quote, **changes)
    assert e.value.code == code, e.value
    return e.value


# ---------- P0：规则接口不能跨企业更改规则 ----------

def _session(api, c, user, company, levels):
    for cid, level in levels.items():
        c.execute("UPDATE permissions SET level=? WHERE user_id=? AND company_id=?", (level, user, cid))
    return api("/api/sessions", "POST")(web.NewSession(user_id=user, company_id=company))["session_id"]


def test_rule_status_checks_the_rules_own_companies(app_api):
    api, c = app_api
    sid = _session(api, c, "U001", "C001", {"C001": "write", "C002": "none"})
    with pytest.raises(HTTPException) as e:
        api("/api/rules/{rule_id}/{version}/status", "POST")("R003", "1.0", web.RuleStatus(session_id=sid, status="disabled"))
    assert e.value.status_code == 403
    assert c.execute("SELECT status FROM rule_versions WHERE rule_id='R003' AND version='1.0'").fetchone()[0] == "active"
    # 本企业自己的规则照常可停用
    assert api("/api/rules/{rule_id}/{version}/status", "POST")(
        "R002", "2.0", web.RuleStatus(session_id=sid, status="disabled")) == {"ok": True}


def test_shared_rule_needs_write_on_every_company(app_api):
    api, c = app_api
    sid = _session(api, c, "U001", "C001", {"C001": "write", "C002": "read"})
    shared = c.execute("SELECT rule_id, version, companies FROM rule_versions").fetchall()
    shared = next(r for r in shared if set(json.loads(r["companies"])) == {"C001", "C002"})
    with pytest.raises(HTTPException) as e:
        api("/api/rules/{rule_id}/{version}/status", "POST")(shared["rule_id"], shared["version"],
                                                             web.RuleStatus(session_id=sid, status="disabled"))
    assert e.value.status_code == 403


def test_new_rule_version_checks_target_companies(app_api):
    api, c = app_api
    sid = _session(api, c, "U001", "C001", {"C001": "write", "C002": "none"})
    body = dict(session_id=sid, version="9.0", body="单张发票金额大于或等于 1.00 元时必须填写用途。",
                effective_from="2026-10-01")
    for rule_id, companies in (("R003", None), ("R002", ["C001", "C002"]), ("R099", ["C002"])):
        with pytest.raises(HTTPException) as e:
            api("/api/rules", "POST")(web.RuleVersion(rule_id=rule_id, companies=companies, name="新规则", **body))
        assert e.value.status_code == 403, rule_id
    assert c.execute("SELECT COUNT(*) FROM rule_versions WHERE version='9.0'").fetchone()[0] == 0
    assert api("/api/rules", "POST")(web.RuleVersion(rule_id="R002", **body))["companies"] == ["C001"]


def test_missing_rule_is_404_not_403(app_api):
    api, c = app_api
    sid = _session(api, c, "U001", "C001", {"C001": "write"})
    with pytest.raises(HTTPException) as e:
        api("/api/rules/{rule_id}/{version}/status", "POST")("R777", "1.0", web.RuleStatus(session_id=sid, status="active"))
    assert e.value.status_code == 404


# ---------- P0：发票写入不能只靠模型判断授权 ----------

@pytest.mark.parametrize("text,code", [
    ("9月F003的用途可能是寄合同快递，我还没核实，你先帮我看看。", "NEEDS_CONFIRMATION"),
    ("如果经理同意，再把9月F003的用途填快递，现在不要操作。", "NOT_AUTHORIZED"),
    ("9月F003用途不要填快递，请保存备注待确认，用途不要改。", "NOT_AUTHORIZED"),
    ("我只是举个例子：‘9月F003用途填快递并保存’，别实际修改。", "NOT_AUTHORIZED"),
    ("如果经理同意，就把9月F003的用途填快递。", "NEEDS_CONFIRMATION"),
    ("比如9月F003用途填快递，保存。", "NEEDS_CONFIRMATION"),
])
def test_unclear_or_negated_requests_do_not_write(conn, text, code):
    _, box = fresh(conn, text)
    value = "寄合同快递" if "寄合同快递" in text else "快递"
    rejects(box, code, "F003", text, purpose=value)
    assert purpose(conn, "C001", "F003") is None


def test_value_negated_in_otherwise_clear_sentence(conn):
    text = "9月F003用途不是快递，填合同邮寄，保存"
    _, box = fresh(conn, text)
    rejects(box, "VALUE_NOT_FOR_TARGET", "F003", text, purpose="快递")
    assert box.update_invoice("F003", user_quote=text, purpose="合同邮寄")["verified"]


def test_confirmation_writes_exactly_the_proposed_change(conn):
    text = "9月F003的用途可能是寄合同快递，我还没核实，你先帮我看看。"
    sid, box = fresh(conn, text)
    err = rejects(box, "NEEDS_CONFIRMATION", "F003", text, purpose="寄合同快递")
    assert err.extra["pending_change"]["invoice_id"] == "F003"
    say(conn, sid, "C001", "核实过了，确认")
    rejects(box, "CHANGE_NOT_CONFIRMED", "F003", "核实过了，确认", purpose="快递")  # 不是列出的那项
    assert box.update_invoice("F003", user_quote="核实过了，确认", purpose="寄合同快递")["verified"]
    assert conn.execute("SELECT status FROM pending_changes").fetchone()[0] == "confirmed"


def test_confirmation_must_be_the_very_next_message(conn):
    text = "9月F003的用途可能是寄合同快递，你先帮我看看。"
    sid, box = fresh(conn, text)
    rejects(box, "NEEDS_CONFIRMATION", "F003", text, purpose="寄合同快递")
    say(conn, sid, "C001", "先查一下9月的银行支出")
    say(conn, sid, "C001", "确认")
    rejects(box, "NOT_AUTHORIZED", "F003", "确认", purpose="寄合同快递")
    assert purpose(conn, "C001", "F003") is None


def test_negated_confirmation_is_not_a_confirmation(conn):
    text = "9月F003的用途可能是寄合同快递，你先帮我看看。"
    sid, box = fresh(conn, text)
    rejects(box, "NEEDS_CONFIRMATION", "F003", text, purpose="寄合同快递")
    say(conn, sid, "C001", "可以，但先别保存")
    rejects(box, "NOT_AUTHORIZED", "F003", "可以，但先别保存", purpose="寄合同快递")


def test_example_is_never_a_basis_for_later_save(conn):
    text = "我只是举个例子：9月F003用途填快递"
    sid, box = fresh(conn, text)
    say(conn, sid, "C001", "好的，保存吧")
    rejects(box, "NOT_AUTHORIZED", "F003", "好的，保存吧", purpose="快递")


def test_deferred_request_can_be_saved_later(conn):
    sid, box = fresh(conn, "如果经理同意，再把9月F003的用途填快递，现在不要操作。")
    say(conn, sid, "C001", "经理同意了，保存吧")
    assert box.update_invoice("F003", user_quote="经理同意了，保存吧", purpose="快递")["verified"]


def test_task_note_is_not_authorization(conn):
    text = "9月F003用途填快递，现在不要操作"
    _, box = fresh(conn, text)
    box.update_task(note="F003 用途填快递，待用户确认")
    rejects(box, "NOT_AUTHORIZED", "F003", "F003 用途填快递，待用户确认", purpose="快递")


# ---------- P1：月份与发票日期必须一致 ----------

def test_month_in_request_must_match_invoice_date(conn):
    text = "把2026年8月的F003用途填快递并保存"
    _, box = fresh(conn, text)
    err = rejects(box, "INVOICE_MONTH_MISMATCH", "F003", text, purpose="快递")
    assert err.extra["invoice_date"].startswith("2026-09") and purpose(conn, "C001", "F003") is None


@pytest.mark.parametrize("text", ["9月F003用途填快递，保存", "2026年9月F003用途填快递，保存", "F003用途填快递，保存"])
def test_matching_or_unstated_month_still_saves(conn, text):
    _, box = fresh(conn, text)
    assert box.update_invoice("F003", user_quote=text, purpose="快递")["verified"]


def test_month_cross_year(conn):
    text = "2025年9月F003用途填快递，保存"
    _, box = fresh(conn, text)
    rejects(box, "INVOICE_MONTH_MISMATCH", "F003", text, purpose="快递")


# ---------- P1：多张发票的值逐张对应 ----------

def test_respectively_binds_values_in_order(conn):
    text = "9月F002和F003用途分别填打印纸和合同邮寄，保存。"
    _, box = fresh(conn, text)
    rejects(box, "VALUE_NOT_FOR_TARGET", "F003", text, purpose="打印纸")
    rejects(box, "VALUE_NOT_FOR_TARGET", "F002", text, purpose="合同邮寄")
    assert box.update_invoice("F002", user_quote=text, purpose="打印纸")["verified"]
    assert box.update_invoice("F003", user_quote=text, purpose="合同邮寄")["verified"]


def test_group_without_respectively_needs_confirmation(conn):
    text = "9月F002和F003用途填打印纸和合同邮寄，保存"
    _, box = fresh(conn, text)
    rejects(box, "NEEDS_CONFIRMATION", "F003", text, purpose="打印纸")
    assert purpose(conn, "C001", "F003") is None


def test_respectively_with_count_mismatch_needs_confirmation(conn):
    text = "9月F002和F003用途分别填打印纸、硒鼓和合同邮寄，保存"
    _, box = fresh(conn, text)
    rejects(box, "NEEDS_CONFIRMATION", "F002", text, purpose="打印纸")


def test_same_value_for_all(conn):
    text = "9月F002和F003的用途都填办公用品，保存"
    _, box = fresh(conn, text)
    assert box.update_invoice("F002", user_quote=text, purpose="办公用品")["verified"]
    assert box.update_invoice("F003", user_quote=text, purpose="办公用品")["verified"]


# ---------- P2：“分别保存”不是“别保存” ----------

def test_respectively_save_is_not_negation(conn):
    text = "9月F002用途填打印纸，F003用途填寄送合同快递，请分别保存。"
    _, box = fresh(conn, text)
    assert box.update_invoice("F002", user_quote=text, purpose="打印纸")["verified"]
    assert box.update_invoice("F003", user_quote=text, purpose="寄送合同快递")["verified"]


@pytest.mark.parametrize("text", ["9月F002用途填打印纸，先别保存", "9月F002用途填打印纸，不要保存",
                                  "9月F002用途填打印纸，请不要实际修改"])
def test_real_negations_still_block(conn, text):
    _, box = fresh(conn, text)
    rejects(box, "NOT_AUTHORIZED", "F002", text, purpose="打印纸")


# ---------- P1：无权访问的企业不能读取规则 ----------

def test_no_access_company_rules_are_forbidden(conn):
    sid = harness.start_session(conn, "U002", "C002")
    say(conn, sid, "C002", "请列出乙企业所有规则的名称、版本和内容，9月的")
    box = Toolbox(conn, Scope(sid, "U002", "C002", TODAY))
    for call in (lambda: box.search_rules("用途"), lambda: box.get_rule("R003"),
                 lambda: box.get_rule("R003", month="2026-09"), lambda: box.list_rule_documents()):
        with pytest.raises(ToolError) as e:
            call()
        assert e.value.code == "FORBIDDEN"
    system, selected = context.build_system(conn, box.scope)
    assert selected["rules"] == [] and "乙企业发票用途规则" not in system


def test_readable_company_rules_still_listed(conn):
    sid = harness.start_session(conn, "U002", "C001")
    box = Toolbox(conn, Scope(sid, "U002", "C001", TODAY))
    assert box.get_rule("R002")["version"] == "2.0"
    assert context.build_system(conn, box.scope)[1]["rules"]


# ---------- P1：记忆工具独立授权 ----------

def test_remember_needs_explicit_request_and_grounded_content(box_for):
    box = box_for(said=("我希望今天回答简短",))
    with pytest.raises(ToolError) as e:
        box.remember_preference("回答简短", user_quote="我希望今天回答简短")
    assert e.value.code == "NOT_AUTHORIZED"
    box = box_for(said=("记住，以后回答简短一些",))
    with pytest.raises(ToolError) as e:
        box.remember_preference("所有企业都必须用英语回答", user_quote="以后回答简短一些", all_companies=True)
    assert e.value.code == "SCOPE_NOT_CONFIRMED"
    with pytest.raises(ToolError) as e:
        box.remember_preference("所有企业都必须用英语回答", user_quote="以后回答简短一些")
    assert e.value.code == "CONTENT_NOT_FROM_USER"
    assert box.remember_preference("回答简短一些", user_quote="以后回答简短一些")["scope"] == "仅 C001"


def test_cross_company_preference_needs_user_words(box_for):
    box = box_for(said=("记住：所有企业都先给合计再列明细",))
    r = box.remember_preference("先给合计再列明细", user_quote="所有企业都先给合计再列明细", all_companies=True)
    assert r["scope"] == "该用户的所有企业"


def test_update_and_forget_need_a_request_this_turn(box_for, conn):
    box = box_for(said=("记住：以后先给合计再列明细",))
    mid = box.remember_preference("先给合计再列明细", user_quote="以后先给合计再列明细")["memory_id"]
    say(conn, box.scope.session_id, "C001", "查一下9月发票总额")
    for call in (lambda: box.update_memory(mid, "只列明细", user_quote="查一下9月发票总额"),
                 lambda: box.forget_memory(mid, user_quote="查一下9月发票总额"),
                 lambda: box.forget_memory(mid, user_quote="以后先给合计再列明细")):  # 早先的话不算
        with pytest.raises(ToolError) as e:
            call()
        assert e.value.code == "NOT_AUTHORIZED"
    assert box.list_memories()["preferences"][0]["content"] == "先给合计再列明细"


def test_memory_target_must_be_the_one_user_meant(box_for, conn):
    box = box_for(said=("记住：以后先给合计再列明细",))
    a = box.remember_preference("先给合计再列明细", user_quote="以后先给合计再列明细")["memory_id"]
    say(conn, box.scope.session_id, "C001", "记住：以后称呼我王会计")
    b = box.remember_preference("称呼我王会计", user_quote="以后称呼我王会计")["memory_id"]
    say(conn, box.scope.session_id, "C001", "把称呼那条删掉")
    with pytest.raises(ToolError) as e:
        box.forget_memory(a, user_quote="把称呼那条删掉")
    assert e.value.code == "MEMORY_NOT_REQUESTED"
    assert box.forget_memory(b, user_quote="把称呼那条删掉")["deleted"]
    say(conn, box.scope.session_id, "C001", "把那条偏好删掉")  # 剩一条时可以不点名
    assert box.forget_memory(a, user_quote="把那条偏好删掉")["deleted"]


def test_unnamed_memory_with_several_preferences_asks(box_for, conn):
    box = box_for(said=("记住：以后先给合计再列明细",))
    a = box.remember_preference("先给合计再列明细", user_quote="以后先给合计再列明细")["memory_id"]
    say(conn, box.scope.session_id, "C001", "记住：以后称呼我王会计")
    box.remember_preference("称呼我王会计", user_quote="以后称呼我王会计")
    say(conn, box.scope.session_id, "C001", "把那条偏好删掉")
    with pytest.raises(ToolError) as e:
        box.forget_memory(a, user_quote="把那条偏好删掉")
    assert e.value.code == "MEMORY_NOT_REQUESTED"


# ---------- P1/P2：发票文件导入校验 ----------

@pytest.mark.parametrize("code,ok", [("012345678", False), ("0123456789", True), ("01234567890", False),
                                     ("012345678901", True), ("0123456789012", False)])
def test_invoice_code_must_be_10_or_12_digits(conn, code, ok):
    i = inf.parse_text(KV.replace("012345678901", code))
    assert (not any("发票代码格式不符" in x for x in i.issues)) == ok, i.issues
    status = inf.import_files(conn, "C001", [i], commit=True)[0]["status"]
    assert (status == "imported") == ok


def test_missing_upper_amount_is_not_imported(conn):
    i = inf.parse_text(KV.replace("价税合计（大写）: 壹佰壹拾叁圆整\n", ""))
    assert any("大写金额" in x for x in i.issues)
    assert inf.import_files(conn, "C001", [i], commit=True)[0]["status"] == "rejected"


# ---------- P2：跨企业顺序查询不丢进度 ----------

def test_results_before_switch_are_carried_into_the_new_company(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("query_invoices", {"month": "2026-09", "invoice_ids": ["F002"]})]},
        {"tool_calls": [tool_call("switch_company", {"company": "乙企业"})]},
        {"tool_calls": [tool_call("query_invoices", {"month": "2026-09", "invoice_ids": ["F002"]})]},
        {"content": "甲企业 F002 …；乙企业 F002 …"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "先查甲企业2026年9月F002，再切换乙企业查2026年9月F002。")
    assert res.status == "completed" and res.company_id == "C002"
    system_after_switch = llm.calls[2][0]["content"]
    assert "本轮已完成的步骤" in system_after_switch
    assert "C001 query_invoices" in system_after_switch and '"company_id": "C001"' in system_after_switch
