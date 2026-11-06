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
