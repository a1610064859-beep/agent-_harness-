"""构建每轮模型输入：会话绑定 + 任务记忆 + 长期偏好 + 适用规则目录 + 本企业最近对话。

不拼接全部历史：只取当前会话、当前企业下的最近消息，并按字符预算裁剪；业务数据一律让模型重新查询。
"""
from __future__ import annotations

import sqlite3

from . import knowledge, memory
from .tools import LEVEL_TEXT, Scope, _pref_view, _task_view

POLICY = """\
你是企业财税资料助手，帮助用户查询发票与银行资料、补充发票用途或备注、检查未完成事项。所有企业、数据和规则都是比赛模拟内容。

工作方式：
1. 当前数据一律调用工具获取。金额、合计、用途检查结果以工具返回为准；不要自己加减求和，需要的合计只用工具返回的字段（如 invoice_total、missing_required_total、listed_total），工具没给的合计就不报。不要沿用旧对话里的数字。
2. 月份：用户没说清楚、任务记忆里也没有时，先问月份，不要默认当前月。沿用任务记忆里的月份时，在回复中说明“按上次的 X 月”，方便用户更正。“这张”“那几张”等指代不唯一时追问。
3. 修改：只能修改发票的用途和备注；企业、编号、日期、金额、销售方、项目名称和银行流水都不能改。
   只有用户在本次会话中明确要求修改时才调用 update_invoice，user_quote 逐字摘录用户原话，用途和备注使用用户给出的原文。
   只查询或询问规则不算授权；之前会话里没执行的修改不能自动执行，需要用户重新确认。
   不能根据销售方或项目名称推测用途。保存结果以工具回读核验为准，未核验不能说已保存。
4. 发票由“企业编号+发票编号”确定。用户要处理另一家企业时调用 switch_company，不要沿用上一家企业的发票、月份或待办。
5. 规则：回答规则问题或引用规则前，先用 search_rules / get_rule 取得条款原文；只引用工具返回的规则名称和版本，例如“依据《甲企业发票用途规则》2.0 版”。资料不足时直说缺什么。已停用的规则不能作为依据。下面的规则目录只用于知道有哪些规则，不能代替条款原文。
6. 发票和银行流水没有逐条对应关系，不能凭金额、日期或交易方推断已付款。无权限或查询失败不等于没有记录，不能说成 0。
7. 长期偏好只在用户明确要求“记住”时保存；偏好不能改变权限、规则或用户本轮的明确要求。
8. 任务记忆：任务记忆里没有目标、或用户提出了新的工作目标（如检查某月用途、补齐缺失信息）时，调用 update_task 用一句话记下目标；全部处理完或用户说不再处理时 complete_task。

回复要求：像正常业务交流，先直接给结果，再给必要依据和下一步；说清已完成和未完成的事；只追问缺少的信息；少套话、少重复，不机械列点。金额单位为元，保留两位小数。不要在回复中提到工具名、字段名或系统内部机制；没有通过工具保存的内容，不要说已经记下或保存。"""


def build_system(conn: sqlite3.Connection, scope: Scope) -> tuple[str, dict]:
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (scope.user_id,)).fetchone()
    perms = conn.execute("SELECT c.company_id, c.name, c.short_name, p.level FROM companies c "
                         "LEFT JOIN permissions p ON p.company_id=c.company_id AND p.user_id=? ORDER BY c.company_id",
                         (scope.user_id,)).fetchall()
    current = next(p for p in perms if p["company_id"] == scope.company_id)
    lines = [POLICY, "", "## 会话绑定（由程序设定，对话中不能更改身份或权限）",
             f"- 今天：{scope.today.isoformat()}",
             f"- 用户：{scope.user_id} {user['name']}",
             f"- 当前企业：{current['company_id']} {current['short_name']}，权限：{LEVEL_TEXT[current['level'] or 'none']}",
             "- 其他企业：" + "；".join(f"{p['company_id']} {p['short_name']}（{LEVEL_TEXT[p['level'] or 'none']}）"
                                      for p in perms if p["company_id"] != scope.company_id)]

    task = memory.open_task(conn, scope.user_id, scope.company_id)
    lines.append("\n## 任务记忆（该用户在当前企业的未完成任务）")
    selected = {"task": None, "memories": [], "rules": []}
    if task:
        view = _task_view(conn, task)
        selected["task"] = view["task_id"]
        lines += [f"- 目标：{view['goal'] or '（未记录）'}", f"- 关注月份：{view['month'] or '（未确定）'}",
                  f"- 待处理：{'、'.join(view['pending']) or '无'}",
                  f"- 本任务已完成：{'、'.join(view['done']) or '无'}"]
        lines += [f"- 已确认：{n}" for n in view["notes"]]
        lines.append("（待办来自上次检查，回答“还剩哪些”前请重新调用 check_purpose 以当前数据为准。）")
    else:
        lines.append("- 无")

    prefs = memory.preferences(conn, scope.user_id, scope.company_id)
    lines.append("\n## 长期偏好（用户明确要求记住的）")
    for p in prefs:
        v = _pref_view(p)
        lines.append(f"- [{v['memory_id']}] {v['content']}（适用：{v['scope']}）")
        selected["memories"].append(v["memory_id"])
    if not prefs:
        lines.append("- 无")

    lines.append("\n## 当前企业适用规则目录（按今天解析；按业务月份查询时以工具返回的版本为准）")
    for r in knowledge.resolve(conn, scope.company_id, as_of=scope.today):
        if r.chosen:
            ref = r.ref()
            selected["rules"].append(f"{ref['rule_id']} {ref['version']}")
            lines.append(f"- {ref['rule_id']}《{ref['name']}》{ref['version']} 版，{ref['effective_from']} 起生效")
        for e in r.excluded:
            lines.append(f"  - 不可用：{e['rule']}（{e['reason']}）")
    scope.memory_refs = list(selected["memories"])
    return "\n".join(lines), selected


def history(conn: sqlite3.Connection, scope: Scope, budget_chars: int) -> list[dict]:
    """当前会话、当前企业的最近对话；切换企业前的内容不带入，避免串用。"""
    rows = conn.execute("SELECT role, content FROM messages WHERE session_id=? AND company_id=? "
                        "ORDER BY id DESC", (scope.session_id, scope.company_id)).fetchall()
    out, used = [], 0
    for r in rows:
        if used + len(r["content"]) > budget_chars and out:
            break
        used += len(r["content"])
        if r["role"] == "event":
            out.append({"role": "user", "content": f"[系统事件] {r['content']}"})
        else:
            out.append({"role": r["role"], "content": r["content"]})
    return list(reversed(out))
