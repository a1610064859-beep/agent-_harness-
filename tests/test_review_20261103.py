"""2026-11-03 对抗测试发现的问题的回归测试：引用核对重答丢了用户问题、多月合计、
没点名发票的批量填写、PDFium 并发崩溃。仅使用隔离库和脚本化模型。"""
import subprocess
import sys
import textwrap

import pytest

from fiscal_harness import harness, tools
from fiscal_harness.llm import ScriptedLLM, tool_call
from fiscal_harness.tools import ToolError
from .conftest import say


# ---------- 引用核对重答要带上用户本轮原话 ----------

def test_citation_retry_restates_the_users_question(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([
        {"content": "依据《银行资料说明》1.0 版，专票可以抵扣。"},  # 本轮没有工具返回该规则
        {"content": "专票可以抵扣进项税，普票一般不能。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "增值税专票和普票有什么区别？")
    retry = llm.calls[1][-1]["content"]
    assert retry.startswith("[程序核对]") and "增值税专票和普票有什么区别？" in retry
    assert res.status == "completed" and res.reply.startswith("专票")


# ---------- 多月合计由程序计算 ----------

def test_summarize_months_combines_by_program(box_for):
    box = box_for(said=("8月和9月加起来的发票总额是多少？",))
    out = box.summarize_months(["2026-09", "2026-08", "2026-09"])
    assert out["months"] == ["2026-08", "2026-09"]
    assert [m["invoice_total"] for m in out["by_month"]] == ["9046.35", "9622.06"]
    assert out["combined"]["invoice_total"] == "18668.41"
    assert out["combined"]["invoice_count"] == 20
    # 收入、支出仍分别相加，不互相抵减
    assert out["combined"]["bank_expense_total"] != out["combined"]["bank_income_total"]
    assert {r["rule_id"] for r in out["rules"]} >= {"R001", "R004"}


def test_summarize_months_with_empty_month_is_zero_not_missing(box_for):
    out = box_for().summarize_months(["2026-07", "2026-09"])
    july = out["by_month"][0]
    assert july["month"] == "2026-07" and july["invoice_total"] == "0.00" and july["invoice_count"] == 0
    assert out["combined"]["invoice_total"] == "9622.06"


def test_summarize_months_still_requires_months_from_user(box_for):
    box = box_for(said=("9月的发票总额",))
    with pytest.raises(ToolError) as e:
        box.summarize_months(["2026-08", "2026-09"])
    assert e.value.code == "MONTH_UNCONFIRMED"
    with pytest.raises(ToolError) as e:
        box.summarize_months(["2026-09"])
    assert e.value.code == "BAD_ARG"


def test_summarize_months_respects_permissions(box_for):
    box = box_for(user="U002", company="C002", said=("8月和9月",))
    with pytest.raises(ToolError) as e:
        box.summarize_months(["2026-08", "2026-09"])
    assert e.value.code in ("FORBIDDEN", "NO_ACCESS", "NOT_AUTHORIZED") or "权限" in e.value.message


def test_summarize_months_via_dispatch_and_spec():
    names = {s["function"]["name"] for s in tools.tool_specs()}
    assert "summarize_months" in names


# ---------- 没点名发票的批量填写：逐张记为待确认，下一句确认后写入 ----------

def _purpose(conn, inv, company="C001"):
    return conn.execute("SELECT purpose FROM invoices WHERE company_id=? AND invoice_id=?", (company, inv)).fetchone()[0]


def test_unnamed_bulk_request_becomes_confirmation_then_writes(box_for, conn):
    box = box_for(said=("9月所有没填用途的发票，用途都填办公用品，保存",))
    for inv in ("F002", "F003"):
        with pytest.raises(ToolError) as e:
            box.update_invoice(inv, "9月所有没填用途的发票，用途都填办公用品，保存", purpose="办公用品")
        assert e.value.code == "NEEDS_CONFIRMATION"
        assert e.value.extra["pending_change"]["invoice_id"] == inv
    assert _purpose(conn, "F002") is None and _purpose(conn, "F003") is None
    say(conn, box.scope.session_id, "C001", "确认")
    assert box.update_invoice("F002", "确认", purpose="办公用品")["verified"]
    assert box.update_invoice("F003", "确认", purpose="办公用品")["verified"]
    assert _purpose(conn, "F002") == "办公用品" and _purpose(conn, "F003") == "办公用品"
    with pytest.raises(ToolError) as e:  # 没列出过的发票不能借这句确认写入
        box.update_invoice("F004", "确认", purpose="办公用品")
    assert e.value.code in ("CHANGE_NOT_CONFIRMED", "INVOICE_NOT_REQUESTED")
    assert _purpose(conn, "F004") is None


def test_unnamed_request_value_must_come_from_quote(box_for, conn):
    box = box_for(said=("9月没填的都帮我补上，保存",))
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", "9月没填的都帮我补上，保存", purpose="打印复印纸")
    assert e.value.code == "INVOICE_NOT_REQUESTED"
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0


def test_unnamed_older_message_is_not_a_proposal(box_for, conn):
    box = box_for(said=("9月没填的都填办公用品，保存", "先看看银行流水"))
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", "9月没填的都填办公用品，保存", purpose="办公用品")
    assert e.value.code == "INVOICE_NOT_REQUESTED"
    assert _purpose(conn, "F002") is None


def test_unnamed_request_with_risk_words_is_not_proposed(box_for, conn):
    box = box_for(said=("如果经理同意，9月没填的都填办公用品",))
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", "如果经理同意，9月没填的都填办公用品", purpose="办公用品")
    assert e.value.code == "NOT_AUTHORIZED"


def test_unnamed_request_checks_month(box_for):
    box = box_for(said=("8月没填的都填办公用品，保存",))
    with pytest.raises(ToolError) as e:  # F002 在 C001 是 9 月的票
        box.update_invoice("F002", "8月没填的都填办公用品，保存", purpose="办公用品")
    assert e.value.code == "INVOICE_MONTH_MISMATCH"


# ---------- PDFium 并发渲染不能让进程崩溃 ----------

def test_concurrent_pdf_qr_reading_does_not_crash(tmp_path):
    pytest.importorskip("zxingcpp")
    pytest.importorskip("pypdfium2")
    pytest.importorskip("reportlab")
    # 放在子进程里：修复失效时子进程 abort，测试失败而不是拖垮整个 pytest
    script = textwrap.dedent("""
        from concurrent.futures import ThreadPoolExecutor
        from tests.test_invoice_qr import _pdf_with_qr, QR
        from fiscal_harness import ocr
        data = _pdf_with_qr(QR)
        with ThreadPoolExecutor(8) as pool:
            got = list(pool.map(lambda _: ocr.read_qr(data), range(64)))
        assert got == [QR] * 64, got[:3]
        print("ok")
    """)
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0 and "ok" in proc.stdout, proc.stderr[-2000:]


# ---------- “F003也一样”：转为待确认，用户回“确认”即可写入 ----------

def test_same_as_reference_becomes_confirmation(box_for, conn):
    q = "9月F002用途填办公耗材，F003也一样，保存"
    box = box_for(said=(q,))
    assert box.update_invoice("F002", q, purpose="办公耗材")["verified"]
    with pytest.raises(ToolError) as e:
        box.update_invoice("F003", q, purpose="办公耗材")
    assert e.value.code == "NEEDS_CONFIRMATION"
    assert _purpose(conn, "F003") is None
    say(conn, box.scope.session_id, "C001", "确认")
    assert box.update_invoice("F003", "确认", purpose="办公耗材")["verified"]


def test_value_for_other_invoice_is_still_rejected(box_for):
    q = "9月F002用途填办公耗材，F003填快递费，保存"
    box = box_for(said=(q,))
    with pytest.raises(ToolError) as e:
        box.update_invoice("F003", q, purpose="办公耗材")
    assert e.value.code == "VALUE_NOT_FOR_TARGET"


# ---------- 月份出处：常见说法要认得出，且不能认错 ----------

@pytest.mark.parametrize("text,expected", [
    ("今年9月的发票总额", {"2026-09"}),
    ("去年12月", {"2025-12"}),
    ("26年9月", {"2026-09"}),
    ("2025年九月", {"2025-09"}),
    ("上上个月", {"2026-08"}),
    ("上个月", {"2026-09"}),
    ("8、9月", {"2026-08", "2026-09"}),
    ("7至9月", {"2026-07", "2026-08", "2026-09"}),
    ("今年第三季度", {"2026-07", "2026-08", "2026-09"}),
    ("Q3 2026", {"2026-07", "2026-08", "2026-09"}),
    ("What's the invoice total for September 2026?", {"2026-09"}),
    ("please list them separately, may I ask", set()),
    ("9月8日的F002", {"2026-09"}),
    ("F006 金额1000.01元，三张发票", set()),
])
def test_mentioned_months(text, expected):
    from datetime import date
    assert tools.mentioned_months([text], date(2026, 10, 26)) == expected


def test_quoting_the_old_request_after_confirmation_is_redirected(box_for, conn):
    q = "9月所有没填用途的发票，用途都填办公用品，保存"
    box = box_for(said=(q,))
    with pytest.raises(ToolError):
        box.update_invoice("F002", q, purpose="办公用品")
    say(conn, box.scope.session_id, "C001", "确认，都按办公用品保存")
    with pytest.raises(ToolError) as e:  # 模型误把旧原话当出处：程序指明应引用的确认原话，不写入
        box.update_invoice("F002", q, purpose="办公用品")
    assert e.value.code == "QUOTE_THE_CONFIRMATION" and "确认，都按办公用品保存" in e.value.message
    assert _purpose(conn, "F002") is None
    assert box.update_invoice("F002", "确认，都按办公用品保存", purpose="办公用品")["verified"]


def _assistant(conn, sid, text, company="C001"):
    conn.execute("INSERT INTO messages (session_id, company_id, role, content, created_at) VALUES (?,?,?,?,?)",
                 (sid, company, "assistant", text, "2026-10-26T10:00:01"))


def test_confirming_what_the_assistant_listed_writes(box_for, conn):
    q = "9月所有没填用途的发票，用途都填办公用品，保存"
    box = box_for(said=(q,))
    sid = box.scope.session_id
    _assistant(conn, sid, "这 5 张统一填“办公用品”，请确认：F002、F003、F004、F006、F009。")
    say(conn, sid, "C001", "确认")
    assert box.update_invoice("F002", "确认", purpose="办公用品")["verified"]
    with pytest.raises(ToolError):  # 助手没列出的发票不能借这句确认
        box.update_invoice("F001", "确认", purpose="办公用品")


def test_assistant_listing_alone_cannot_supply_the_value(box_for, conn):
    box = box_for(said=("9月还有哪些没填用途？",))
    sid = box.scope.session_id
    _assistant(conn, sid, "还剩 F002、F003，建议 F002 填“办公用品”。")
    say(conn, sid, "C001", "好的，保存")
    with pytest.raises(ToolError):  # “办公用品”只出自助手，不是用户原话
        box.update_invoice("F002", "好的，保存", purpose="办公用品")
    assert _purpose(conn, "F002") is None


def test_assistant_listing_without_the_value_does_not_bind(box_for, conn):
    box = box_for(said=("9月还有哪些没填用途？",))
    sid = box.scope.session_id
    _assistant(conn, sid, "F002 已经填了打印纸。还剩 F003、F004 没填。")
    say(conn, sid, "C001", "都填快递，保存")
    with pytest.raises(ToolError) as e:  # F002 出现在回复里但回复没提“快递”：不直接写，至多转为待确认
        box.update_invoice("F002", "都填快递，保存", purpose="快递")
    assert e.value.code in ("NEEDS_CONFIRMATION", "INVOICE_NOT_REQUESTED")
    assert _purpose(conn, "F002") is None


# ---------- 金额出处：模型自己算出的数字不能直接发给用户 ----------

def test_self_computed_amount_triggers_retry(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("summarize_month", {"month": "2026-09"})]},
        {"content": "9月银行支出 7180.74 元；收支相抵净额 5819.76 元。"},
        {"content": "9月银行支出 7,180.74 元，收入和支出分别统计，不相抵。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "9月银行支出总额是多少？是不是就等于收入减支出？")
    retry = llm.calls[2][-1]["content"]
    assert "5819.76" in retry and "不要自己加减" in retry
    assert res.status == "completed" and not res.anomalies and "5819.76" not in res.reply


def test_persisting_unverified_amount_is_held(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    bad = {"content": "9月净额 5819.76 元。"}
    llm = ScriptedLLM([{"tool_calls": [tool_call("summarize_month", {"month": "2026-09"})]}, bad, bad])
    res = harness.run_turn(conn, llm, settings, sid, "9月收支怎么样？")
    assert res.status == "needs_review" and "5819.76" not in res.reply
    assert any("5819.76" in a for a in res.anomalies)


def test_amounts_from_tools_user_and_history_are_fine(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    harness.run_turn(conn, ScriptedLLM([
        {"tool_calls": [tool_call("summarize_month", {"month": "2026-09"})]},
        {"content": "9月发票总额 9622.06 元。"}]), settings, sid, "9月发票总额")
    res = harness.run_turn(conn, ScriptedLLM([{"content": "刚才说的 9,622.06 元是9月发票总额；你提到的 88.80 元我没有查到对应记录。2026.09 起执行。"}]),
                           settings, sid, "那 88.80 元那张呢？")
    assert res.status == "completed" and not res.anomalies, res.anomalies


def test_rule_lookup_with_unconfirmed_month_falls_back_to_today(box_for):
    box = box_for(said=("差旅住宿标准是多少？",))
    out = box.search_rules("住宿标准", month="2026-09")
    assert "month_note" in out
    assert "month_note" in box.get_rule("R002", month="2026-09")
    assert "month_note" not in box.get_rule("R002")
