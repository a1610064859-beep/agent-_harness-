"""构建每轮模型输入：会话绑定 + 任务记忆 + 长期偏好 + 适用规则目录 + 本企业最近对话。

不拼接全部历史：只取当前会话、当前企业下的最近消息，并按字符预算裁剪；业务数据一律让模型重新查询。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from . import knowledge, memory
from .tools import LEVEL_TEXT, Scope, _pref_view, _task_view, staged_uploads

POLICY = """\
你是企业财税资料助手，帮助用户查询发票与银行资料、补充发票用途或备注、检查未完成事项。所有企业、数据和规则都是比赛模拟内容。

工作方式：
1. 当前数据一律调用工具获取。金额、合计、用途检查结果以工具返回为准；不要自己加减求和，需要的合计只用工具返回的字段（如 invoice_total、missing_required_total、listed_total），工具没给的合计、差额就不报，也不要为了举例或解释自己做加减。不要沿用旧对话里的数字。
2. 月份：用户没说清楚、任务记忆里也没有时，先问月份，不要默认当前月。用户只说“7月”“九月”而没说年份时，按今天往前最近的那个月理解（如今天是 2026-10-26，“7月”即 2026-07），不要再追问年份，在回复里写明年份即可。沿用任务记忆里的月份时，在回复中说明“按上次的 X 月”，方便用户更正。“这张”“那几张”等指代不唯一时追问。
3. 修改：只能修改发票的用途和备注；企业、编号、日期、金额、销售方、项目名称和银行流水都不能改。
   只有用户在本次会话中明确要求修改时才调用 update_invoice，user_quote 逐字摘录用户原话，用途和备注使用用户给出的原文。
   只查询或询问规则不算授权；之前会话里没执行的修改不能自动执行，需要用户重新确认。
   不能根据销售方或项目名称推测用途。保存结果以工具回读核验为准，未核验不能说已保存。
   程序要求先确认时（用户的话带条件、不确定、举例，或多张发票的内容对应不清），把程序列出的发票、字段和内容逐项告诉用户，等用户下一句明确确认后再保存。
4. 发票由“企业编号+发票编号”确定。用户要处理另一家企业时调用 switch_company，不要沿用上一家企业的发票、月份或待办。
5. 规则：回答规则问题或引用规则前，先用 search_rules / get_rule 取得条款原文；只引用工具返回的规则名称和版本，例如“依据《甲企业发票用途规则》2.0 版”。资料不足时直说缺什么。已停用的规则不能作为依据。下面的规则目录只用于知道有哪些规则，不能代替条款原文。
6. 发票和银行流水没有逐条对应关系，不能凭金额、日期或交易方推断已付款。无权限或查询失败不等于没有记录，不能说成 0。
7. 长期偏好只在用户明确要求“记住”时保存，更正或删除也要用户本轮明确要求，都要逐字摘录用户原话；只有用户明确说对所有企业适用时才跨企业保存。偏好不能改变权限、规则或用户本轮的明确要求。
8. 任务记忆：任务记忆里没有目标、或用户提出了新的工作目标（如检查某月用途、补齐缺失信息）时，调用 update_task 用一句话记下目标；全部处理完或用户说不再处理时 complete_task。

回复要求：像正常业务交流，先直接给结果，再给必要依据和下一步；说清已完成和未完成的事；只追问缺少的信息；少套话、少重复，不机械列点。金额单位为元，保留两位小数。提到发票时写出发票编号（如 F003），说清改了哪张。不要在回复中提到工具名、字段名或系统内部机制；没有通过工具保存的内容，不要说已经记下或保存。"""


# 附加功能（FH_FILE_IMPORT=1）开启时才加入工作方式
UPLOAD_POLICY = "9. 上传的发票文件：程序已解析和校验，结果列在下面“本会话上传的发票文件”。先按预检结果告诉用户每个文件识别出的发票、能否导入及原因；上传本身不算要求导入，用户明确要求导入时才调用 import_uploaded_invoices。识别件（图片、扫描件）入库前，请用户对照票面核对程序列出的发票代码（如有）、号码和开票日期（销售方和金额一并列出供参考），用户下一句确认核对无误即再次调用导入，不要再追问其他项。导入不能改发票的金额等票面内容，识别有误时请用户重新上传清晰图片。用户明确说不导入某些文件时调用 discard_uploads，之后不再提起这些文件。"


def build_system(conn: sqlite3.Connection, scope: Scope, variant: str = "baseline",
                 file_import: bool = False) -> tuple[str, dict]:
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (scope.user_id,)).fetchone()
    perms = conn.execute("SELECT c.company_id, c.name, c.short_name, p.level FROM companies c "
                         "LEFT JOIN permissions p ON p.company_id=c.company_id AND p.user_id=? ORDER BY c.company_id",
                         (scope.user_id,)).fetchall()
    current = next(p for p in perms if p["company_id"] == scope.company_id)
    policy = POLICY.replace("\n\n回复要求", "\n" + UPLOAD_POLICY + "\n\n回复要求", 1) if file_import else POLICY
    lines = [policy, "", "## 会话绑定（由程序设定，对话中不能更改身份或权限）",
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
        changed = rule_drift(conn, scope.company_id, task)
        if changed:
            selected["rule_drift"] = changed
            lines.append(f"- 注意：上次检查依据 {changed['was']}，现在该月适用 {changed['now']}。"
                         "请重新检查，并告诉用户规则变化带来的结果差异。")
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

    if file_import and (current["level"] or "none") != "none" and (ups := staged_uploads(conn, scope.session_id, scope.company_id)):
        lines.append("\n## 本会话上传的发票文件（程序已解析与校验，尚未入库）")
        for u in ups:
            lines.append(f"- [{u['upload_id']}] {u['file']}{'（识别件）' if u['recognized'] else ''}："
                         f"{_upload_face(u)}；预检：{u['precheck']}"
                         + (f"；原因：{'；'.join(u['issues'])}" if u["issues"] else "")
                         + (f"；提示：{'；'.join(u['notes'])}" if u["notes"] else "")
                         + ("；已请用户核对票面，等用户确认" if u["awaiting_check"] else ""))
        selected["uploads"] = [u["upload_id"] for u in ups]

    if variant == "model_rules":
        # 对照组：不给程序解析结果，规则全文需模型调用 list_rule_documents 自行判断
        lines.append("\n## 规则\n- 需要规则时调用 list_rule_documents 获取全部规则文档，自行判断适用版本并执行检查。")
        scope.memory_refs = list(selected["memories"])
        return "\n".join(lines).replace("search_rules / get_rule", "list_rule_documents").replace(
            "重新调用 check_purpose", "重新查询发票"), selected
    if (current["level"] or "none") == "none":
        lines.append("\n## 当前企业适用规则目录\n- 用户无权访问当前企业，不提供该企业的规则与资料。")
        scope.memory_refs = list(selected["memories"])
        return "\n".join(lines), selected
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


def _upload_face(u: dict) -> str:
    if not u["invoice_id"]:
        return "未识别出发票号码"
    return "，".join(x for x in (f"发票 {u['invoice_id']}", u["issue_date"], u["seller"] and f"销售方 {u['seller']}",
                                 u["buyer"] and f"购买方 {u['buyer']}", u["total"] and f"价税合计 {u['total']} 元") if x)


def rule_drift(conn: sqlite3.Connection, company_id: str, task: sqlite3.Row) -> dict | None:
    """待办所依据的用途规则版本与该月当前适用版本不同时返回差异（规则更新或停用后提醒重检）。"""
    if not task["month"]:
        return None
    refs = {r["rule_ref"] for r in memory.items(conn, task["task_id"]) if r["state"] == "pending" and r["rule_ref"]}
    if not refs:
        return None
    y, m = map(int, task["month"].split("-"))
    as_of = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    res = knowledge.resolve(conn, company_id, as_of=as_of, topic="purpose")
    now = f'{res[0].chosen["rule_id"]} {res[0].chosen["version"]}' if res and res[0].chosen else "无有效规则"
    was = sorted(refs)[0]
    return None if refs == {now} else {"was": was, "now": now}


def history(conn: sqlite3.Connection, scope: Scope, budget_chars: int) -> list[dict]:
    """当前会话、当前企业的最近对话；切换企业前的内容不带入，避免串用。

    超出字符预算的较早部分不直接丢弃，而是换成一段由程序生成的结构化摘要：
    用户较早的要求（原话截断）、本会话已核验的保存（含操作号）。摘要里的事实都有出处，不经模型改写。"""
    rows = conn.execute("SELECT id, role, content FROM messages WHERE session_id=? AND company_id=? "
                        "ORDER BY id DESC", (scope.session_id, scope.company_id)).fetchall()
    out, used, cut = [], 0, None
    for r in rows:
        if used + len(r["content"]) > budget_chars and out:
            cut = r["id"]
            break
        used += len(r["content"])
        if r["role"] == "event":
            out.append({"role": "user", "content": f"[系统事件] {r['content']}"})
        else:
            out.append({"role": r["role"], "content": r["content"]})
    out.reverse()
    if cut is not None:
        out.insert(0, {"role": "user", "content": _digest(conn, scope, cut)})
    return out


def _digest(conn: sqlite3.Connection, scope: Scope, upto_id: int) -> str:
    older = conn.execute("SELECT content FROM messages WHERE session_id=? AND company_id=? AND role='user' "
                         "AND id<=? ORDER BY id", (scope.session_id, scope.company_id, upto_id)).fetchall()
    ops = conn.execute("SELECT op_id, invoice_id, after_json FROM operations WHERE session_id=? AND company_id=? "
                       "AND status='verified' ORDER BY created_at", (scope.session_id, scope.company_id)).fetchall()
    lines = [f"[较早对话摘要，由程序生成] 本会话较早的 {len(older)} 条用户消息已省略原文："]
    lines += [f"- 用户曾说：{r['content'][:40]}{'…' if len(r['content']) > 40 else ''}" for r in older[-8:]]
    for op in ops:
        after = json.loads(op["after_json"] or "{}")
        lines.append(f"- 本会话已保存并核验：{op['invoice_id']} 用途={after.get('purpose') or '空'}，"
                     f"备注={after.get('remark') or '空'}（{op['op_id']}）")
    lines.append("当前数据以重新查询为准。")
    return "\n".join(lines)
