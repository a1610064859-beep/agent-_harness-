"""复赛向能力：中断恢复、旧库迁移、长对话摘要、规则变化提醒。"""
import sqlite3

import pytest

from fiscal_harness import context, db, harness, knowledge, memory
from fiscal_harness.llm import ScriptedLLM
from fiscal_harness.tools import Scope, SimulatedCrash, recover_pending

from .conftest import TODAY, say


def test_crash_after_commit_is_recovered_on_restart(box_for, conn, monkeypatch):
    box = box_for(said=("9月 F003 用途填寄送合同快递，保存",))
    box.check_purpose("2026-09")
    monkeypatch.setenv("FH_FAULT", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        box.update_invoice("F003", purpose="寄送合同快递", user_quote="F003 用途填寄送合同快递")
    assert conn.execute("SELECT status FROM operations").fetchone()[0] == "pending"
    monkeypatch.delenv("FH_FAULT")
    done = recover_pending(conn)
    assert done == [{"op_id": done[0]["op_id"], "invoice": "C001/F003", "verified": True}]
    op = conn.execute("SELECT status, recovered_at FROM operations").fetchone()
    assert op["status"] == "verified" and op["recovered_at"]
    task = memory.open_task(conn, "U001", "C001")
    states = {r["invoice_id"]: r["state"] for r in memory.items(conn, task["task_id"])}
    assert states["F003"] == "done"
    assert "程序中断" in task["notes"] and "保存成功" in task["notes"]
    assert recover_pending(conn) == []  # 只核对一次，不会重复提交


def test_recovery_reports_mismatch(box_for, conn, monkeypatch):
    box = box_for(said=("F009 用途写设备维修配件",))
    monkeypatch.setenv("FH_FAULT", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        box.update_invoice("F009", purpose="设备维修配件", user_quote="F009 用途写设备维修配件")
    monkeypatch.delenv("FH_FAULT")
    conn.execute("UPDATE invoices SET purpose='别的内容' WHERE company_id='C001' AND invoice_id='F009'")
    assert recover_pending(conn)[0]["verified"] is False
    assert conn.execute("SELECT status FROM operations").fetchone()[0] == "mismatch"


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE operations (op_id TEXT PRIMARY KEY, idempotency_key TEXT, run_id TEXT, session_id TEXT,"
                " user_id TEXT, company_id TEXT, invoice_id TEXT, before_json TEXT, after_json TEXT, status TEXT,"
                " created_at TEXT)")
    raw.commit()
    raw.close()
    cols = {r["name"] for r in db.connect(path).execute("PRAGMA table_info(operations)")}
    assert {"intended_json", "recovered_at"} <= cols


def test_long_dialogue_keeps_digest(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    for i in range(30):
        say(conn, sid, "C001", f"第{i}轮：9月的情况再看一下，顺便说明 F00{i % 9 + 1} 的事" + "。" * 200)
    scope = Scope(sid, "U001", "C001", TODAY)
    msgs = context.history(conn, scope, budget_chars=2000)
    assert msgs[0]["content"].startswith("[较早对话摘要，由程序生成]")
    assert sum(len(m["content"]) for m in msgs[1:]) <= 2000
    assert "第0轮" in msgs[0]["content"] or "用户曾说" in msgs[0]["content"]


def test_digest_lists_verified_saves(box_for, conn):
    box = box_for(said=("F002 用途写打印复印纸，保存",) + tuple("填充" * 300 for _ in range(10)))
    box.update_invoice("F002", purpose="打印复印纸", user_quote="F002 用途写打印复印纸")
    digest = context.history(conn, box.scope, budget_chars=700)[0]["content"]
    assert "F002 用途=打印复印纸" in digest and "OP-" in digest


def test_rule_change_triggers_recheck_notice(box_for, conn):
    box = box_for("U001", "C002")
    first = box.check_purpose("2026-09")
    assert [i["invoice_id"] for i in first["missing_required"]] == ["F002", "F005", "F006"]
    knowledge.add_version(conn, "R003", "1.1", "单张发票金额大于或等于 500.00 元时必须填写用途。",
                          effective_from="2026-09-01")
    task = memory.open_task(conn, "U001", "C002")
    assert context.rule_drift(conn, "C002", task) == {"was": "R003 1.0", "now": "R003 1.1"}
    system, selected = context.build_system(conn, box.scope)
    assert "上次检查依据 R003 1.0，现在该月适用 R003 1.1" in system
    second = box.check_purpose("2026-09")
    assert second["rule_changed_since_last_check"] == ["R003 1.0"]
    assert [i["invoice_id"] for i in second["missing_required"]] == ["F001", "F002", "F004", "F005", "F006"]
    assert context.rule_drift(conn, "C002", memory.open_task(conn, "U001", "C002")) is None
