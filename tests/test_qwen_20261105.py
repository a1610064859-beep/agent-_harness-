"""2026-11-05 在官方跑测模型 Qwen3.8-Max 上评测发现的问题，转为确定性回归测试（脚本化模型，不调用真实模型）。"""
from fiscal_harness import harness
from fiscal_harness.llm import ScriptedLLM, tool_call


def test_citation_retry_names_the_rule_id(conn, settings):
    """L1：千问沿用前几轮查过的《甲企业发票用途规则》2.0 版却没在本轮重查；重答提示须点出编号 R002，
    模型据此补查后放行。"""
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([
        {"content": "银行流水不能改。依据《甲企业发票用途规则》2.0 版，每张发票都要填用途。"},
        {"tool_calls": [tool_call("get_rule", {"rule_id": "R002", "month": "2026-09"})]},
        {"content": "银行流水不能改。依据《甲企业发票用途规则》2.0 版，每张发票都要填用途。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "银行流水能改吗？")
    retry = llm.calls[1][-1]["content"]
    assert retry.startswith("[程序核对]") and "R002" in retry and "get_rule" in retry
    assert res.status == "completed" and res.anomalies == []


def test_citation_hint_never_names_other_company_rules(conn, settings):
    """甲企业会话里引用乙企业规则：提示不能把乙企业的规则编号带进模型输入，仍按原样扣下。"""
    sid = harness.start_session(conn, "U001", "C001")
    bad = {"content": "依据《乙企业发票用途规则》1.0 版，1000 元以上才要填。"}
    llm = ScriptedLLM([bad, bad])
    res = harness.run_turn(conn, llm, settings, sid, "用途规则是什么？")
    assert "R003" not in llm.calls[1][-1]["content"]
    assert res.status == "needs_review"


def test_citation_hint_skips_rules_already_returned(conn, settings):
    """只给没通过核对的规则编号，本轮已查到的不重复提示。"""
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("get_rule", {"rule_id": "R001", "month": "2026-09"})]},
        {"content": "依据《金额统计说明》1.0 版和《甲企业发票用途规则》2.0 版。"},
        {"content": "依据《金额统计说明》1.0 版。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "9 月怎么统计？")
    retry = llm.calls[2][-1]["content"]
    assert "即 R002" in retry and "即 R001" not in retry
    assert res.status == "completed"


def test_attachment_hint_only_in_model_input(conn, settings):
    """U1：千问说“没拿到识别结果”。模型输入里的附件行点明预检结果在哪；存储的消息与界面不变。"""
    import dataclasses
    from evals.uploads import build
    from fiscal_harness import context
    s = dataclasses.replace(settings, file_import=True)
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([{"content": "这张是测试服务有限公司开的，价税合计 113.00 元。"}])
    harness.run_turn(conn, llm, s, sid, "看看这张发票是哪家开的", attachments=build(("kv",)))
    sent = llm.calls[0][-1]["content"]
    assert sent.endswith(context.ATTACH_HINT) and "本会话上传的发票文件" in llm.calls[0][0]["content"]
    stored = conn.execute("SELECT content FROM messages WHERE session_id=? AND role='user'", (sid,)).fetchone()[0]
    assert context.ATTACH_HINT not in stored


# ---- L2（2026-11-05 全量评测）：只调 complete_task 就说“已保存，回读一致”，F003 实际没写入 ----

def test_save_claim_without_write_is_held(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    lie = {"content": "已保存。2026 年 9 月 F003 的用途现在写的是“寄送合同的快递费”，保存后回读核对一致。"}
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("update_task", {"goal": "补 F003 用途", "month": "2026-09"})]},
        {"tool_calls": [tool_call("complete_task", {"summary": "F003 的用途已按用户确认保存为寄送合同的快递费。"})]},
        lie, lie,
    ])
    res = harness.run_turn(conn, llm, settings, sid, "9月F003用途就按寄送合同的快递费，保存")
    tool_results = [m for m in llm.calls[2] if m["role"] == "tool"]
    assert "SAVE_NOT_VERIFIED" in tool_results[-1]["content"]          # 总结不能把没保存的写成已保存
    assert conn.execute("SELECT status FROM tasks WHERE user_id='U001' AND company_id='C001'").fetchone()[0] == "open"
    assert "[程序核对]" in llm.calls[3][-1]["content"] and "update_invoice" in llm.calls[3][-1]["content"]
    assert res.status == "needs_review" and "已保存" not in res.reply
    assert conn.execute("SELECT purpose FROM invoices WHERE company_id='C001' AND invoice_id='F003'").fetchone()[0] in (None, "")


def test_save_claim_retry_that_really_saves_passes(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    text = "9月F003用途填寄送合同的快递费，保存"
    llm = ScriptedLLM([
        {"content": "F003 已保存。"},
        {"tool_calls": [tool_call("update_invoice", {"invoice_id": "F003", "user_quote": text, "purpose": "寄送合同的快递费"})]},
        {"content": "F003 的用途已保存为“寄送合同的快递费”，回读核对一致。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, text)
    assert res.status == "completed" and res.anomalies == []


def test_save_claim_check_ignores_negations_past_and_preferences(conn, settings):
    from fiscal_harness import tools
    sid = harness.start_session(conn, "U001", "C001")
    scope = harness.session_scope(conn, sid, settings.today)
    scope.run_id = "R-test"
    for ok_text in ("好的，F003 没有保存，等你确认。", "F003 先不保存。", "F002、F004 是上次已经填好的。",
                    "已记住你的偏好。9 月还剩 F003、F006、F009 没填用途。", "任务已更新，还剩 F003。",
                    "F003 用途还没填，确认后我再保存。", "其余 5 张已填好用途。", "9月共 12 张发票，其中 4 张已填好用途。"):
        assert tools.unverified_save_claims(conn, scope, ok_text) == [], ok_text
    assert tools.unverified_save_claims(conn, scope, "F003 的用途已改为快递费。") == ["F003"]
    assert tools.unverified_save_claims(conn, scope, "两张都已保存并核对无误：F002、F004。") == ["F002", "F004"]
