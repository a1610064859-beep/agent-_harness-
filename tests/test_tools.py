"""确定性回归：标准答案取自《示例数据》明细（与表内核对数一致），不依赖模型。"""
import pytest

from fiscal_harness import knowledge, memory
from fiscal_harness.tools import ToolError, dispatch

from .conftest import TODAY, say

GOLD = {
    ("C001", "2026-08"): (10, "9046.35", "5720.99", "14850.75"),
    ("C001", "2026-09"): (10, "9622.06", "7180.74", "13000.50"),
    ("C002", "2026-08"): (10, "13667.05", "9341.10", "18800.25"),
    ("C002", "2026-09"): (10, "12667.24", "8087.24", "13700.75"),
    ("C001", "2026-07"): (0, "0.00", "0.00", "0.00"),
    ("C002", "2026-07"): (0, "0.00", "0.00", "0.00"),
}


@pytest.mark.parametrize("key", GOLD)
def test_month_totals(box_for, key):
    company, month = key
    r = box_for(company=company).summarize_month(month)
    assert (r["invoice_count"], r["invoice_total"], r["bank_expense_total"], r["bank_income_total"]) == GOLD[key]
    assert {x["rule_id"] for x in r["rules"]} == {"R001", "R004"}


def test_empty_month_says_no_records(box_for):
    r = box_for().summarize_month("2026-07")
    assert r["note"] and "没有" in r["note"]


def test_purpose_check_c001_uses_v2_not_disabled_v1(box_for):
    r = box_for("U001", "C001").check_purpose("2026-09")
    assert (r["rule"]["rule_id"], r["rule"]["version"]) == ("R002", "2.0")
    assert [i["invoice_id"] for i in r["missing_required"]] == ["F002", "F003", "F004", "F006", "F009"]
    assert {"rule": "R002 甲企业发票用途规则 1.0 版", "reason": "已停用"} in r["excluded_versions"]


def test_purpose_check_c002_threshold_inclusive(box_for):
    r = box_for("U001", "C002").check_purpose("2026-09")
    assert (r["rule"]["rule_id"], r["rule"]["version"]) == ("R003", "1.0")
    assert [i["invoice_id"] for i in r["missing_required"]] == ["F002", "F005", "F006"]
    assert r["blank_but_not_required"] == ["F001", "F004", "F009"]


def test_purpose_check_only_september(box_for):
    with pytest.raises(ToolError) as e:
        box_for().check_purpose("2026-08")
    assert e.value.code == "OUT_OF_SCOPE"


def test_month_must_come_from_user(box_for):
    box = box_for(said=("查一下发票总额",))
    with pytest.raises(ToolError) as e:
        box.summarize_month("2026-10")
    assert e.value.code == "MONTH_UNCONFIRMED"


@pytest.mark.parametrize("text,month", [("九月的发票", "2026-09"), ("上个月的银行支出", "2026-09"),
                                        ("2026年8月", "2026-08"), ("8月份", "2026-08")])
def test_month_mentions(box_for, text, month):
    assert box_for(said=(text,)).summarize_month(month)["month"] == month


def test_u002_cannot_view_c002_and_not_zero(box_for):
    box = box_for("U002", "C002")
    res = dispatch(box, "summarize_month", {"month": "2026-09"})
    assert not res["ok"] and res["error"]["code"] == "FORBIDDEN" and "不能回答为零" in res["error"]["message"]


def test_u002_read_only_on_c001(box_for):
    box = box_for("U002", "C001", said=("F002 用途填打印纸，保存",))
    res = dispatch(box, "update_invoice", {"invoice_id": "F002", "purpose": "打印纸", "user_quote": "F002 用途填打印纸"})
    assert res["error"]["code"] == "FORBIDDEN"


def test_switch_company_permission(box_for):
    box = box_for("U002", "C001")
    with pytest.raises(ToolError) as e:
        box.switch_company("乙企业")
    assert e.value.code == "FORBIDDEN" and box.scope.company_id == "C001"


def test_update_saves_and_verifies_only_this_company(box_for, conn):
    box = box_for("U001", "C001", said=("9月 F002 用途写打印复印纸，帮我保存",))
    box.check_purpose("2026-09")
    r = box.update_invoice("F002", purpose="打印复印纸", user_quote="F002 用途写打印复印纸")
    assert r["verified"] and r["after"]["purpose"] == "打印复印纸" and r["open_item_closed"]
    other = conn.execute("SELECT purpose FROM invoices WHERE company_id='C002' AND invoice_id='F002'").fetchone()
    assert other["purpose"] is None
    op = conn.execute("SELECT status FROM operations WHERE op_id=?", (r["op_id"],)).fetchone()
    assert op["status"] == "verified"
    again = box.check_purpose("2026-09")
    assert "F002" not in [i["invoice_id"] for i in again["missing_required"]]


def test_duplicate_submission_writes_once(box_for, conn):
    box = box_for(said=("F004 用途：客户接待住宿，保存",))
    args = dict(invoice_id="F004", purpose="客户接待住宿", user_quote="F004 用途：客户接待住宿")
    first = box.update_invoice(**args)
    second = box.update_invoice(**args)
    assert first["verified"] and second["duplicate"]
    assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    assert conn.execute("SELECT version FROM invoices WHERE company_id='C001' AND invoice_id='F004'").fetchone()[0] == 2


def test_cannot_guess_purpose_from_item(box_for):
    box = box_for(said=("F002 的用途你看着填一下吧，保存",))
    res = dispatch(box, "update_invoice", {"invoice_id": "F002", "purpose": "复印纸、打印服务",
                                           "user_quote": "F002 的用途你看着填一下吧"})
    assert res["error"]["code"] == "CONTENT_NOT_FROM_USER"


def test_previous_session_draft_not_authorized(box_for, conn):
    old = box_for(said=("F003 用途写快递寄送合同",))
    new = box_for(said=("继续处理吧",))
    res = dispatch(new, "update_invoice", {"invoice_id": "F003", "purpose": "快递寄送合同",
                                           "user_quote": "F003 用途写快递寄送合同"})
    assert res["error"]["code"] == "NOT_AUTHORIZED"
    assert old.scope.session_id != new.scope.session_id


def test_only_purpose_and_remark_writable(box_for):
    box = box_for(said=("把 F002 金额改成 900",))
    res = dispatch(box, "update_invoice", {"invoice_id": "F002", "amount": "900", "user_quote": "把 F002 金额改成 900"})
    assert not res["ok"] and res["error"]["code"] == "BAD_ARG"


def test_readback_mismatch_is_not_success(box_for, conn):
    box = box_for(said=("F009 用途写设备维修配件",))
    conn.execute("""CREATE TRIGGER sabotage AFTER UPDATE ON invoices BEGIN
                    UPDATE invoices SET purpose='被改掉' WHERE rowid=NEW.rowid AND NEW.purpose='设备维修配件'; END""")
    r = box.update_invoice("F009", purpose="设备维修配件", user_quote="F009 用途写设备维修配件")
    assert r["verified"] is False and "不能报告保存成功" in r["message"]
    assert conn.execute("SELECT status FROM operations").fetchone()[0] == "mismatch"


def test_one_open_task_per_user_company(box_for, conn):
    box_for().check_purpose("2026-09")
    box_for().check_purpose("2026-09")
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id='U001' AND company_id='C001' "
                        "AND status='open'").fetchone()[0] == 1


def test_task_memory_isolated_by_company_and_user(box_for, conn):
    box_for("U001", "C001").check_purpose("2026-09")
    t_c002 = memory.open_task(conn, "U001", "C002")
    t_u002 = memory.open_task(conn, "U002", "C001")
    assert t_c002 is None and t_u002 is None


def test_preferences_scope_update_delete(box_for, conn):
    box = box_for(said=("记住：以后先给合计再列明细",))
    mid = box.remember_preference("先给合计再列明细", user_quote="以后先给合计再列明细")["memory_id"]
    assert [p["memory_id"] for p in box_for("U001", "C001").list_memories()["preferences"]] == [mid]
    assert box_for("U001", "C002").list_memories()["preferences"] == []
    assert box_for("U002", "C001").list_memories()["preferences"] == []
    say(conn, box.scope.session_id, "C001", "把先给合计那条改成：先给合计，明细只列编号")
    box.update_memory(mid, "先给合计，明细只列编号", user_quote="先给合计，明细只列编号")
    assert box.list_memories()["preferences"][0]["revision"] == 2
    say(conn, box.scope.session_id, "C001", "删掉先给合计这条偏好")
    box.forget_memory(mid, user_quote="删掉先给合计这条偏好")
    assert box.list_memories()["preferences"] == []
    assert conn.execute("SELECT content FROM memories WHERE memory_id=?", (mid,)).fetchone()[0] is None


def test_remember_requires_user_words(box_for):
    res = dispatch(box_for(said=("查9月发票",)), "remember_preference",
                   {"content": "用户喜欢简短回答", "user_quote": "我喜欢简短回答"})
    assert res["error"]["code"] == "NOT_AUTHORIZED"


def test_rule_update_and_disable(conn):
    knowledge.add_version(conn, "R003", "2.0", "单张发票金额大于或等于 500.00 元时必须填写用途。",
                          effective_from="2026-10-01")
    sept = knowledge.resolve(conn, "C002", as_of=TODAY.replace(month=9, day=30), topic="purpose")[0]
    octo = knowledge.resolve(conn, "C002", as_of=TODAY, topic="purpose")[0]
    assert sept.chosen["version"] == "1.0" and octo.chosen["version"] == "2.0"
    assert octo.chosen["params"] == '{"purpose_required_min_cents": 50000}'
    knowledge.set_status(conn, "R003", "2.0", "disabled")
    assert knowledge.resolve(conn, "C002", as_of=TODAY, topic="purpose")[0].chosen["version"] == "1.0"


def test_search_rules_only_active_versions(box_for):
    r = box_for(said=("9月发票用途有什么要求",)).search_rules("发票用途 金额门槛", month="2026-09")
    versions = {(s["rule_id"], s["version"]) for s in r["snippets"]}
    assert ("R002", "1.0") not in versions and ("R002", "2.0") in versions
    assert not any(s["rule_id"] == "R003" for s in r["snippets"])  # 乙企业规则不进入甲企业结果
