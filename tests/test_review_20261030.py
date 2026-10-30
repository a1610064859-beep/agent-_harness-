"""2026-10-30 外部评审（c284adf）的 P1 问题：每条复现路径转成回归测试。模型一律用预设脚本，不调用真实接口。"""
import io
import random
from datetime import date

import pytest
from pypdf import PdfReader, PdfWriter

from fiscal_harness import harness, invoice_files as inf, runlog
from fiscal_harness.llm import ScriptedLLM, tool_call
from fiscal_harness.tools import Scope, ToolError, Toolbox
from invoicegen.cli import write_batch
from invoicegen.fake_data import fake_invoice
from invoicegen.models import Party

from .conftest import TODAY, say


def fresh(conn, *texts, user="U001", company="C001"):
    sid = harness.start_session(conn, user, company)
    for t in texts:
        say(conn, sid, company, t)
    return sid, Toolbox(conn, Scope(sid, user, company, TODAY))


def purpose(conn, invoice, company="C001"):
    return conn.execute("SELECT purpose FROM invoices WHERE company_id=? AND invoice_id=?", (company, invoice)).fetchone()[0]


# ---------- 1. 撤销、更正后旧原话不再作为授权 ----------

QUOTE = "9月F002用途填打印纸，保存"


@pytest.mark.parametrize("later", ["算了，F002先不改了", "等一下，刚才说错了，先别保存", "撤销刚才的修改", "取消吧"])
def test_revoked_quote_cannot_authorize(conn, later):
    _, box = fresh(conn, QUOTE, later)
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", user_quote=QUOTE, purpose="打印纸")
    assert e.value.code == "AUTHORIZATION_REVOKED" and purpose(conn, "F002") is None


def test_corrected_quote_is_superseded_by_the_new_instruction(conn):
    later = "F002不是打印纸，用途应该是硒鼓，保存"
    _, box = fresh(conn, QUOTE, later)
    with pytest.raises(ToolError) as e:
        box.update_invoice("F002", user_quote=QUOTE, purpose="打印纸")
    assert e.value.code == "AUTHORIZATION_SUPERSEDED" and purpose(conn, "F002") is None
    assert box.update_invoice("F002", user_quote=later, purpose="硒鼓")["verified"]


def test_unrelated_later_messages_keep_the_quote_valid(conn):
    _, box = fresh(conn, QUOTE, "另外查一下9月银行支出", "F003先不用管")
    assert box.update_invoice("F002", user_quote=QUOTE, purpose="打印纸")["verified"]


def test_revoking_a_confirmation_blocks_it(conn):
    _, box = fresh(conn, "9月F004的用途是客户接待住宿，先别保存，我再想想", "可以了，保存吧", "不对，还是算了")
    with pytest.raises(ToolError) as e:
        box.update_invoice("F004", user_quote="可以了，保存吧", purpose="客户接待住宿")
    assert e.value.code == "AUTHORIZATION_REVOKED" and purpose(conn, "F004") is None


def test_revocation_through_the_run_loop(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    harness.run_turn(conn, ScriptedLLM([{"content": "好的，请确认后我再保存。"}]), settings, sid, QUOTE)
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("update_invoice", {"invoice_id": "F002", "purpose": "打印纸", "user_quote": QUOTE})]},
        lambda msgs: {"content": "已撤销，F002 没有修改。" if "AUTHORIZATION_REVOKED" in msgs[-1]["content"]
                      else "已保存。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "算了，F002不改了")
    assert res.tool_calls[0]["error"] == "AUTHORIZATION_REVOKED" and purpose(conn, "F002") is None
    assert "没有修改" in res.reply


# ---------- 2. 规则引用核对失败时不报“完成”，也不发出未核实的结论 ----------

BAD = {"content": "依据《甲企业发票用途规则》1.0 版，1000 元以下不用填。"}


def test_failed_citation_check_holds_the_reply(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    res = harness.run_turn(conn, ScriptedLLM([BAD, BAD]), settings, sid, "用途规则是什么？")
    assert res.status == "needs_review" and res.anomalies
    assert "1000 元以下不用填" not in res.reply and "未经核对" in res.reply
    run = runlog.load(conn, res.run_id)
    assert run["status"] == "needs_review" and run["final_reply"] == res.reply
    held = [s for s in run["steps"] if s["kind"] == "held_reply"]
    assert held and "1000 元以下不用填" in held[0]["payload"]["reply"]
    stored = conn.execute("SELECT content FROM messages WHERE run_id=? AND role='assistant'", (res.run_id,)).fetchone()
    assert "1000 元以下不用填" not in stored[0]


def test_step_limit_fallback_with_bad_citation_is_held(conn, settings):
    import dataclasses
    s = dataclasses.replace(settings, max_steps=3)
    sid = harness.start_session(conn, "U001", "C001")
    query = {"tool_calls": [tool_call("summarize_month", {"month": "2026-09"})]}
    res = harness.run_turn(conn, ScriptedLLM([BAD, query, query]), s, sid, "用途规则是什么？")
    assert res.status == "needs_review" and "1000 元以下不用填" not in res.reply


def test_verified_citation_still_completes(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("summarize_month", {"month": "2026-09"})]},
        {"content": "9 月发票总额 9622.06 元，依据《金额统计说明》1.0 版。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "甲企业 9 月发票总额多少？")
    assert res.status == "completed" and "9622.06" in res.reply


# ---------- 3. 一个 PDF 里有多张发票时明确拒收，不能只导入第一张 ----------

BUYER = Party(name="甲企业（比赛模拟）", tax_id="TEST0000000000C001")


def _merged(tmp_path, n=2):
    rng = random.Random(3)
    invs = [fake_invoice(rng, i + 1, date(2026, 9, 30), buyer=BUYER, days=29) for i in range(n)]
    write_batch(invs, tmp_path)
    w = PdfWriter()
    for inv in invs:
        for page in PdfReader(tmp_path / f"{inv.number}.pdf").pages:
            w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return [inv.number for inv in invs], buf.getvalue()


def test_pdf_with_two_invoices_is_rejected(tmp_path):
    numbers, data = _merged(tmp_path)
    inv = inf.parse_bytes("merged.pdf", data)
    assert any("2 张发票" in i for i in inv.issues), inv.issues
    assert all(n in " ".join(inv.issues) for n in numbers)


def test_single_invoice_pdf_still_parses(tmp_path):
    _, data = _merged(tmp_path, n=1)
    inv = inf.parse_bytes("one.pdf", data)
    assert not any("张发票" in i for i in inv.issues), inv.issues


def test_multi_page_scan_is_rejected_before_ocr(tmp_path):
    w = PdfWriter()
    w.add_blank_page(595, 842)
    w.add_blank_page(595, 842)
    buf = io.BytesIO()
    w.write(buf)
    called = []
    inv = inf.parse_bytes("scan.pdf", buf.getvalue(), ocr=lambda name, data: called.append(name) or "")
    assert not called and any("2 页" in i for i in inv.issues), inv.issues
