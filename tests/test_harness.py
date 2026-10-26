"""运行循环的确定性测试：用脚本化模型固定模型输出，检查循环、记录、守卫与跨会话行为。
这里不评价模型能力，真实模型评测见 evals/。"""
import json

from fiscal_harness import harness, runlog
from fiscal_harness.llm import ScriptedLLM, tool_call


def test_turn_records_input_tools_and_reply(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("summarize_month", {"month": "2026-09"})]},
        {"content": "9 月发票总额 9622.06 元，依据《金额统计说明》1.0 版。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "甲企业 9 月发票总额多少？")
    assert res.status == "completed" and res.anomalies == []
    run = runlog.load(conn, res.run_id)
    kinds = [s["kind"] for s in run["steps"]]
    assert kinds == ["llm_input", "llm_output", "tool", "llm_input", "llm_output"]
    first_input = run["steps"][0]["payload"]["messages"]
    assert first_input[0]["role"] == "system" and "U001" in first_input[0]["content"]
    assert first_input[-1] == {"role": "user", "content": "甲企业 9 月发票总额多少？"}
    tool_step = run["steps"][2]["payload"]
    assert tool_step["result"]["data"]["invoice_total"] == "9622.06"
    assert "R001 1.0" in run["selected"]["rules_cited_by_tools"]


def test_citation_not_returned_by_tools_is_flagged(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([{"content": "依据《甲企业发票用途规则》1.0 版，1000 元以下不用填。"}])
    res = harness.run_turn(conn, llm, settings, sid, "用途规则是什么？")
    assert res.anomalies and "1.0" in res.anomalies[0]


def test_history_does_not_cross_company(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    harness.run_turn(conn, ScriptedLLM([
        {"tool_calls": [tool_call("check_purpose", {"month": "2026-09"})]},
        {"content": "甲企业 9 月缺用途：F002、F003、F004、F006、F009。"},
    ]), settings, sid, "9 月哪些发票没填用途？")
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("switch_company", {"company": "乙企业"})]},
        {"content": "已切换到乙企业。"},
    ])
    res = harness.run_turn(conn, llm, settings, sid, "切到乙企业")
    assert res.company_id == "C002"
    llm2 = ScriptedLLM([{"content": "好的"}])
    harness.run_turn(conn, llm2, settings, sid, "乙企业这边呢？")
    sent = json.dumps(llm2.calls[0], ensure_ascii=False)
    assert "F003、F004" not in sent  # 甲企业的回复没有带进乙企业的上下文
    assert "当前企业：C002" in llm2.calls[0][0]["content"]


def test_new_session_keeps_task_memory_and_preferences(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    harness.run_turn(conn, ScriptedLLM([
        {"tool_calls": [tool_call("check_purpose", {"month": "2026-09"}),
                        tool_call("remember_preference", {"content": "先给合计再列明细",
                                                          "user_quote": "记住以后先给合计再列明细"})]},
        {"content": "已检查并记住偏好。"},
    ]), settings, sid, "检查 9 月用途，另外记住以后先给合计再列明细")
    sid2 = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([{"tool_calls": [tool_call("check_purpose", {"month": "2026-09"})]}, {"content": "还剩 5 张。"}])
    res = harness.run_turn(conn, llm, settings, sid2, "还剩哪些没处理？")
    system = llm.calls[0][0]["content"]
    assert "先给合计再列明细" in system and "关注月份：2026-09" in system
    assert "F002、F003、F004、F006、F009" in system
    assert res.tool_calls[0]["ok"]  # 月份来自任务记忆，不需要用户在新会话重说


def test_step_limit(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    loop = [{"tool_calls": [tool_call("list_memories", {}, f"c{i}")]} for i in range(settings.max_steps)]
    res = harness.run_turn(conn, ScriptedLLM(loop), settings, sid, "随便看看")
    assert res.status == "step_limit"


def test_api_key_never_logged(conn, settings, monkeypatch):
    fake = "sk" + "-test1234567890abcdef"  # 拼接生成，避免源码本身被密钥扫描命中
    monkeypatch.setenv("DEEPSEEK_API_KEY", fake)
    sid = harness.start_session(conn, "U001", "C001")
    harness.run_turn(conn, ScriptedLLM([{"content": f"密钥是 {fake}"}]), settings, sid, "hi")
    dump = json.dumps([dict(r) for r in conn.execute("SELECT payload FROM run_steps")], ensure_ascii=False)
    assert fake not in dump
