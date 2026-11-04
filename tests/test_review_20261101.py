"""记忆删除与规则引用检查的回归测试；仅使用隔离库和脚本化模型。"""
import json

import pytest

from fiscal_harness import harness, memory, runlog, tools
from fiscal_harness.llm import ScriptedLLM, tool_call
from .conftest import say


def record(conn, run_id, user='U001', company='C001', payload=None):
    sid = harness.start_session(conn, user, company)
    log = runlog.RunLog(conn, run_id, sid, user, company, '查询', 'scripted')
    log.step('tool', payload)
    log.finish(reply='完成', status='completed', company_id=company, selected={}, usage={}, anomalies=[])
    return conn.execute('SELECT payload FROM run_steps WHERE run_id=?', (run_id,)).fetchone()[0]


def forget(conn, settings, text, company='C001'):
    sid = harness.start_session(conn, 'U001', company)
    mid = memory.add_preference(conn, 'U001', company, text)
    say(conn, sid, company, '删除这条偏好')
    box = tools.Toolbox(conn, harness.session_scope(conn, sid, settings.today))
    assert box.forget_memory(mid, '删除这条偏好')['deleted']
    return mid


def test_numeric_preference_keeps_json_and_business_values(conn, settings):
    record(conn, 'R-own', payload={'content': '偏好：100；门槛1000.00元',
                                  'amount': '100.00', 'invoice_id': '100', 'version': 100,
                                  'arguments': json.dumps({'content': '100', 'amount_cents': 100000})})
    mid = forget(conn, settings, '100')
    payload = runlog.load(conn, 'R-own')['steps'][0]['payload']
    assert f'[已删除的记忆 {mid}]' in payload['content']
    assert '1000.00元' in payload['content']
    assert (payload['amount'], payload['invoice_id'], payload['version']) == ('100.00', '100', 100)
    nested = json.loads(payload['arguments'])
    assert nested['amount_cents'] == 100000 and '100' not in nested['content']


def test_forgetting_cannot_change_another_user_or_company(conn, settings):
    original_user = record(conn, 'R-other-user', user='U002', payload={'content': '只用中文'})
    original_company = record(conn, 'R-other-company', company='C002', payload={'content': '只用中文'})
    record(conn, 'R-own', payload={'content': '只用中文'})
    forget(conn, settings, '只用中文')
    for rid, expected in [('R-other-user', original_user), ('R-other-company', original_company)]:
        assert conn.execute('SELECT payload FROM run_steps WHERE run_id=?', (rid,)).fetchone()[0] == expected
    assert '已删除' in runlog.load(conn, 'R-own')['steps'][0]['payload']['content']


def test_global_preference_cleans_only_its_owner_across_companies(conn, settings):
    sid = harness.start_session(conn, 'U001', 'C001')
    mid = memory.add_preference(conn, 'U001', None, '只用中文')
    record(conn, 'R-global', company='C002', payload={'content': '只用中文'})
    foreign = record(conn, 'R-foreign', user='U002', payload={'content': '只用中文'})
    say(conn, sid, 'C001', '删除这条偏好')
    tools.Toolbox(conn, harness.session_scope(conn, sid, settings.today)).forget_memory(mid, '删除这条偏好')
    assert '已删除' in runlog.load(conn, 'R-global')['steps'][0]['payload']['content']
    assert conn.execute("SELECT payload FROM run_steps WHERE run_id='R-foreign'").fetchone()[0] == foreign


def test_deleted_preference_not_in_next_model_input_or_new_log(conn, settings):
    sid = harness.start_session(conn, 'U001', 'C001')
    text = '称呼用户为王会计'
    mid = memory.add_preference(conn, 'U001', 'C001', text)
    llm = ScriptedLLM([
        {'tool_calls': [tool_call('list_memories', {}), tool_call('forget_memory',
                         {'memory_id': mid, 'user_quote': '删除这条偏好'})]},
        {'content': '已删除。'},
    ])
    result = harness.run_turn(conn, llm, settings, sid, '删除这条偏好')
    assert result.status == 'completed' and result.tool_calls[-1]['ok']
    assert text in llm.calls[0][0]['content']
    assert text not in json.dumps(llm.calls[1], ensure_ascii=False)
    loaded = runlog.load(conn, result.run_id)
    inputs = [s['payload'] for s in loaded['steps'] if s['kind'] == 'llm_input']
    assert text not in json.dumps(inputs, ensure_ascii=False)
    assert mid not in loaded['selected']['memories']


def test_switch_after_forget_preserves_other_company_active_preference(conn, settings):
    sid = harness.start_session(conn, 'U001', 'C001')
    text = '回答简短'
    mid = memory.add_preference(conn, 'U001', 'C001', text)
    other = memory.add_preference(conn, 'U001', 'C002', text)
    llm = ScriptedLLM([
        {'tool_calls': [tool_call('forget_memory', {'memory_id': mid, 'user_quote': '删除这条偏好'}),
                        tool_call('switch_company', {'company': 'C002'})]},
        {'content': '已切换。'},
    ])
    result = harness.run_turn(conn, llm, settings, sid, '删除这条偏好，然后切换到乙企业')
    assert result.status == 'completed'
    assert mid not in llm.calls[1][0]['content']
    assert other in llm.calls[1][0]['content'] and text in llm.calls[1][0]['content']


BAD_CITATIONS = [
    '依据R002 1.0版，1000元以下不用填用途。',
    '依据R002 v1.0，1000元以下不用填用途。',
    '依据R002（1.0版），1000元以下不用填用途。',
    '依据r002的版本1.0，1000元以下不用填用途。',
    '依据《甲企业发票用途规则》v1.0，1000元以下不用填用途。',
    '依据《甲企业发票用途规则》（版本1.0），1000元以下不用填用途。',
    '依据《甲企业发票用途规则》1.0，1000元以下不用填用途。',
]


@pytest.mark.parametrize('reply', BAD_CITATIONS)
def test_alternate_bad_citation_is_held(conn, settings, reply):
    sid = harness.start_session(conn, 'U001', 'C001')
    result = harness.run_turn(conn, ScriptedLLM([{'content': reply}, {'content': reply}]), settings, sid,
                              '甲企业9月用途规则是什么？')
    assert result.status == 'needs_review' and result.anomalies
    assert '1000元以下' not in result.reply


@pytest.mark.parametrize('reply', [
    '依据R002 v2.0，每张必填。',
    '依据《甲企业发票用途规则》（版本2.0），每张必填。',
    '依据r002的2.0版，每张必填。',
])
def test_alternate_good_citation_uses_tool_evidence(conn, settings, reply):
    sid = harness.start_session(conn, 'U001', 'C001')
    llm = ScriptedLLM([{'tool_calls': [tool_call('get_rule', {'rule_id': 'R002', 'month': '2026-09'})]},
                       {'content': reply}])
    result = harness.run_turn(conn, llm, settings, sid, '甲企业9月用途规则是什么？')
    assert result.status == 'completed' and not result.anomalies


# ---------- 补充：删除后复述原文、金额日期不当作版本号、更正后重建 system ----------

def test_reply_echoing_deleted_preference_is_scrubbed(conn, settings):
    sid = harness.start_session(conn, 'U001', 'C001')
    text = '称呼用户为王会计'
    mid = memory.add_preference(conn, 'U001', 'C001', text)
    llm = ScriptedLLM([
        {'tool_calls': [tool_call('forget_memory', {'memory_id': mid, 'user_quote': '删除这条偏好'})]},
        {'content': f'已删除偏好“{text}”。'},
    ])
    result = harness.run_turn(conn, llm, settings, sid, '删除这条偏好')
    assert result.status == 'completed' and text not in result.reply and mid in result.reply
    assert text not in json.dumps(runlog.load(conn, result.run_id), ensure_ascii=False)
    stored = [r[0] for r in conn.execute('SELECT content FROM messages WHERE session_id=?', (sid,))]
    assert not any(text in m for m in stored)


def test_forgetting_scrubs_earlier_history_and_run_text(conn, settings):
    sid = harness.start_session(conn, 'U001', 'C001')
    text = '回答只用中文'
    harness.run_turn(conn, ScriptedLLM([{'content': f'好的，以后{text}。'}]), settings, sid, '你好')
    mid = memory.add_preference(conn, 'U001', 'C001', text)
    llm = ScriptedLLM([
        {'tool_calls': [tool_call('forget_memory', {'memory_id': mid, 'user_quote': '删除这条偏好'})]},
        {'content': '已删除。'},
    ])
    harness.run_turn(conn, llm, settings, sid, '删除这条偏好')
    assert text not in json.dumps(llm.calls[1], ensure_ascii=False)
    nxt = ScriptedLLM([{'content': '好的。'}])
    harness.run_turn(conn, nxt, settings, sid, '继续')
    assert text not in json.dumps(nxt.calls[0], ensure_ascii=False)
    assert not conn.execute('SELECT 1 FROM runs WHERE instr(final_reply, ?)>0', (text,)).fetchone()


def test_update_memory_refreshes_system_within_turn(conn, settings):
    sid = harness.start_session(conn, 'U001', 'C001')
    mid = memory.add_preference(conn, 'U001', 'C001', '回答简短')
    llm = ScriptedLLM([
        {'tool_calls': [tool_call('update_memory', {'memory_id': mid, 'content': '回答详细',
                                                    'user_quote': '把这条偏好改成回答详细'})]},
        {'content': '已更正。'},
    ])
    result = harness.run_turn(conn, llm, settings, sid, '把这条偏好改成回答详细')
    assert result.tool_calls[0]['ok'], result.tool_calls
    assert '回答详细' in llm.calls[1][0]['content'] and '回答简短' not in llm.calls[1][0]['content']


@pytest.mark.parametrize('reply', [
    '依据R002 2.0版，R002 1000.00元以上的发票每张必填用途。',
    '依据R002 2.0版，R002 2026.09 起执行，每张必填。',
    '依据《甲企业发票用途规则》2.0版，按《甲企业发票用途规则》 1000.00 元为界的旧规定已不适用。',
    '依据《甲企业发票用途规则》2.0版和R002 2.0版，每张必填。',
])
def test_amounts_and_dates_are_not_versions(conn, settings, reply):
    sid = harness.start_session(conn, 'U001', 'C001')
    llm = ScriptedLLM([{'tool_calls': [tool_call('get_rule', {'rule_id': 'R002', 'month': '2026-09'})]},
                       {'content': reply}])
    # 提问里带上 1000.00：本测试只看版本号解析，金额出处由 test_review_20261103 覆盖
    result = harness.run_turn(conn, llm, settings, sid, '甲企业9月用途规则是什么？以前是不是1000.00元为界？')
    assert result.status == 'completed' and not result.anomalies, result.anomalies


def test_adjacent_citations_are_both_checked():
    refs = [{'rule_id': 'R002', 'name': '甲企业发票用途规则', 'version': '2.0'}]
    bad = harness._check_citations('《甲企业发票用途规则》2.0《甲企业发票用途规则》1.0', refs)
    assert bad == ['回复引用了《甲企业发票用途规则》1.0 版，但本轮工具没有返回该版本']
