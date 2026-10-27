"""命令行入口：python -m fiscal_harness <命令>。对话、规则管理、记忆管理、运行记录与数据核对都在这里。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import date
from pathlib import Path

from . import db, harness, importer, invoice_files, knowledge, memory, runlog
from .config import load_settings
from .llm import LLMError, from_settings
from .money import fmt
from .tools import ToolError, Toolbox, _pref_view, _task_view, recover_pending


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _conn(settings, recover: bool = False):
    if not settings.db_path.exists():
        sys.exit(f"数据库不存在：{settings.db_path}\n请先运行：python -m fiscal_harness init")
    conn = db.connect(settings.db_path)
    if recover:
        for r in recover_pending(conn):
            print(f"[恢复] {r['op_id']} {r['invoice']}：中断前的保存已回读核验，结果"
                  f"{'一致，保存成功' if r['verified'] else '与预期不一致，未保存成功'}")
    return conn


def cmd_recover(args, s):
    conn = _conn(s)
    done = recover_pending(conn)
    print("没有需要核对的中断写入。" if not done else json.dumps(done, ensure_ascii=False, indent=2))


# ---------- 初始化 ----------

def cmd_init(args, s):
    if s.db_path.exists() and not args.reset:
        sys.exit(f"{s.db_path} 已存在。要恢复为初始资料请加 --reset（会清空保存的修改、记忆、任务与运行记录）。")
    conn = db.connect(s.db_path)
    counts = importer.reset_and_import(conn, s.sample_xlsx, s.rules_docx)
    print(f"已导入 {s.sample_xlsx.name} 与 {s.rules_docx.name} → {s.db_path}")
    _out(counts)


# ---------- 对话 ----------

HELP = """命令：/company C002 切换企业（同 /user 需新会话）  /new 新会话  /run 查看上一轮运行记录  /memory 查看记忆  /quit 退出"""


def cmd_chat(args, s):
    conn = _conn(s, recover=True)
    llm = from_settings(s)
    sid = args.session or harness.start_session(conn, args.user, args.company)
    print(f"会话 {sid}｜用户 {args.user}｜企业 {args.company}｜模型 {llm.model}\n{HELP}")
    last_run = None
    while True:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/new":
            scope = harness.session_scope(conn, sid, s.today)
            sid = harness.start_session(conn, scope.user_id, scope.company_id)
            print(f"已开始新会话 {sid}（任务记忆与长期偏好保留，本会话的对话从头开始）")
            continue
        if text.startswith("/company"):
            parts = text.split()
            if len(parts) != 2:
                print("用法：/company C002")
                continue
            scope = harness.session_scope(conn, sid, s.today)
            try:
                res = Toolbox(conn, scope).switch_company(parts[1])
            except ToolError as e:
                print(e.message)
                continue
            print(f"当前企业：{res['company_id']} {res['name']}（{res['permission']}）")
            continue
        if text == "/run":
            if last_run:
                _print_run(runlog.load(conn, last_run), full=False)
            continue
        if text == "/memory":
            scope = harness.session_scope(conn, sid, s.today)
            _out(Toolbox(conn, scope).list_memories())
            continue
        try:
            res = harness.run_turn(conn, llm, s, sid, text)
        except LLMError as e:
            print(f"[模型错误] {e}")
            continue
        last_run = res.run_id
        trail = "，".join(f"{c['tool']}{'' if c['ok'] else '✗' + (c['error'] or '')}" for c in res.tool_calls)
        print(f"\n助手> {res.reply}")
        print(f"   ⤷ 运行 {res.run_id}｜企业 {res.company_id}｜工具：{trail or '无'}"
              + (f"｜异常：{'; '.join(res.anomalies)}" if res.anomalies else ""))


def cmd_ask(args, s):
    conn = _conn(s, recover=True)
    llm = from_settings(s)
    sid = args.session or harness.start_session(conn, args.user, args.company)
    res = harness.run_turn(conn, llm, s, sid, args.message)
    if args.json:
        _out({"session_id": sid, **res.__dict__})
    else:
        print(res.reply)
        print(f"\n[会话 {sid}｜运行 {res.run_id}｜工具 {[c['tool'] for c in res.tool_calls]}]")


# ---------- 规则 ----------

def cmd_rules(args, s):
    conn = _conn(s)
    if args.action == "list":
        rows = conn.execute("SELECT * FROM rule_versions ORDER BY rule_id, effective_from, version").fetchall()
        for r in rows:
            print(f"{r['rule_id']}  {r['version']:>4}  {r['status']:<8}  {r['effective_from']}  "
                  f"{','.join(json.loads(r['companies']))}  {r['name']}  参数 {r['params']}")
    elif args.action == "show":
        sql, a = "SELECT * FROM rule_versions WHERE rule_id=?", [args.rule_id.upper()]
        if args.version:
            sql, a = sql + " AND version=?", a + [args.version]
        for r in conn.execute(sql + " ORDER BY effective_from", a):
            print(f"== {r['rule_id']}《{r['name']}》{r['version']} 版｜{r['status']}｜生效 {r['effective_from']}")
            for i, p in enumerate(r["body"].split("\n"), 1):
                print(f"  [{i}] {p}")
    elif args.action == "search":
        _out(knowledge.search(conn, args.company, args.query, as_of=_as_of(args)))
    elif args.action == "resolve":
        for r in knowledge.resolve(conn, args.company, as_of=_as_of(args)):
            print(f"{r.ref()}  排除：{r.excluded}")
    elif args.action == "import":
        for rid, ver, st in knowledge.import_file(conn, Path(args.file)):
            print(f"{rid} {ver}: {st}")
    elif args.action == "update":
        body = Path(args.body_file).read_text(encoding="utf-8").strip()
        params = None
        if args.param:
            params = {k: (int(v) if v.lstrip("-").isdigit() else v) for k, v in (p.split("=", 1) for p in args.param)}
        rule = knowledge.add_version(conn, args.rule_id.upper(), args.version, body, name=args.name,
                                     companies=args.companies.split(",") if args.companies else None,
                                     effective_from=args.effective_from, params=params)
        print(f"已新增 {rule['rule_id']} {rule['version']} 版，生效 {rule['effective_from']}，参数 {rule['params']}")
    elif args.action in ("disable", "enable"):
        knowledge.set_status(conn, args.rule_id.upper(), args.version,
                             "disabled" if args.action == "disable" else "active")
        print(f"{args.rule_id} {args.version} 版已{'停用' if args.action == 'disable' else '启用'}")


def _as_of(args) -> date:
    if getattr(args, "month", None):
        y, m = map(int, args.month.split("-"))
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        return date.fromordinal(nxt.toordinal() - 1)
    return date.fromisoformat(args.as_of) if getattr(args, "as_of", None) else date.today()


# ---------- 记忆 ----------

def cmd_memory(args, s):
    conn = _conn(s)
    if args.action == "list":
        _out({"preferences": [_pref_view(p) for p in memory.preferences(conn, args.user, args.company)],
              "open_task": (lambda t: _task_view(conn, t) if t else None)(memory.open_task(conn, args.user, args.company))})
    elif args.action == "edit":
        ok = memory.update_preference(conn, args.memory_id, args.user, args.company, args.content)
        print("已更正" if ok else "找不到该记忆（注意用户与企业范围）")
    elif args.action == "delete":
        ok = memory.delete_preference(conn, args.memory_id, args.user, args.company)
        print("已删除" if ok else "找不到该记忆（注意用户与企业范围）")


# ---------- 运行记录 ----------

def cmd_runs(args, s):
    conn = _conn(s)
    if args.action == "list":
        for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (args.limit,)):
            print(f"{r['run_id']}  {r['started_at']}  {r['user_id']}/{r['company_id']}  {r['status']:<10} "
                  f"{r['user_message'][:40]}")
    elif args.action == "export":
        ids = list(args.run_id)
        if args.session:
            ids += [r["run_id"] for r in conn.execute("SELECT run_id FROM runs WHERE session_id=? ORDER BY started_at",
                                                      (args.session,))]
        if args.all:
            ids += [r["run_id"] for r in conn.execute("SELECT run_id FROM runs ORDER BY started_at")]
        if not ids:
            sys.exit("请指定 run_id、--session 或 --all")
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        md = io.StringIO()
        md.write("# 关键运行记录\n\n每条记录的完整模型输入、工具参数与结果见同目录 JSON。数据库中的修改见文末“保存操作”。\n")
        for rid in dict.fromkeys(ids):
            run = runlog.load(conn, rid)
            (out / f"{rid}.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
            md.write(f"\n## {rid}\n\n```text\n")
            with contextlib.redirect_stdout(md):
                _print_run(run, full=False)
            md.write("```\n")
        ops = conn.execute("SELECT * FROM operations ORDER BY created_at").fetchall()
        md.write("\n## 保存操作（写后回读）\n\n| 操作 | 时间 | 用户 | 发票 | 状态 | 运行 | 修改前 → 后 |\n|---|---|---|---|---|---|---|\n")
        for o in ops:
            b, a = json.loads(o["before_json"]), json.loads(o["after_json"] or "{}")
            md.write(f"| {o['op_id']} | {o['created_at']} | {o['user_id']} | {o['company_id']}/{o['invoice_id']} | "
                     f"{o['status']} | {o['run_id']} | 用途 {b.get('purpose') or '空'} → {a.get('purpose') or '空'}；"
                     f"备注 {b.get('remark') or '空'} → {a.get('remark') or '空'} |\n")
        (out / "README.md").write_text(md.getvalue(), encoding="utf-8")
        print(f"已导出 {len(dict.fromkeys(ids))} 条运行记录到 {out}")
    else:
        run = runlog.load(conn, args.run_id[0] if args.run_id else "")
        if not run:
            sys.exit(f"没有运行记录 {args.run_id}")
        if args.json:
            _out(run)
        else:
            _print_run(run, full=args.full)


def _print_run(run: dict, full: bool) -> None:
    print(f"运行 {run['run_id']}｜会话 {run['session_id']}｜{run['user_id']}/{run['company_id']}｜{run['status']}｜"
          f"模型 {run['model']}｜用量 {run['usage']}")
    print(f"用户：{run['user_message']}")
    print(f"选用：{json.dumps(run['selected'], ensure_ascii=False)}")
    for st in run["steps"]:
        p = st["payload"]
        if st["kind"] == "llm_input":
            msgs = p["messages"]
            if full:
                print(f"--[{st['seq']}] 模型输入（{len(msgs)} 条消息）")
                for m in msgs:
                    print(f"    <{m['role']}> {json.dumps(m.get('content'), ensure_ascii=False)[:2000] if m.get('content') else ''}"
                          f"{' tool_calls=' + json.dumps(m['tool_calls'], ensure_ascii=False) if m.get('tool_calls') else ''}")
            else:
                print(f"--[{st['seq']}] 模型输入：{len(msgs)} 条消息，{sum(len(str(m.get('content') or '')) for m in msgs)} 字符"
                      "（--full 查看全文）")
        elif st["kind"] == "llm_output":
            m = p["message"]
            calls = [f"{c['function']['name']}({c['function']['arguments']})" for c in m.get("tool_calls", [])]
            print(f"--[{st['seq']}] 模型输出：{'调用 ' + '; '.join(calls) if calls else m.get('content', '')[:300]}")
        elif st["kind"] == "tool":
            r = p["result"]
            body = r.get("data") if r["ok"] else r["error"]
            print(f"--[{st['seq']}] 工具 {p['name']} @{p['company_id']} → {'成功' if r['ok'] else '拒绝/失败'}："
                  f"{json.dumps(body, ensure_ascii=False)[:600 if not full else 100000]}")
        else:
            print(f"--[{st['seq']}] {st['kind']}：{json.dumps(p, ensure_ascii=False)}")
    print(f"回复：{run['final_reply']}")
    if run["anomalies"]:
        print(f"异常：{run['anomalies']}")


# ---------- 数据核对 ----------

STATUS_TEXT = {"would_import": "可导入", "imported": "已导入", "duplicate": "已存在，跳过", "conflict": "同号冲突",
               "rejected": "未通过校验"}


def cmd_data(args, s):
    conn = _conn(s)
    if args.action == "import-files":
        if not args.files:
            sys.exit("请给出发票文件或目录，如：data import-files --company C001 --files 发票/")
        if args.accept_buyer_mismatch and not s.allow_foreign_buyer:
            sys.exit("--accept-buyer-mismatch 只用于测试外部样本，需同时设置环境变量 FH_ALLOW_FOREIGN_BUYER=1")
        parsed = [invoice_files.parse_file(f) for f in invoice_files.collect(args.files)]
        results = invoice_files.import_files(conn, args.company, parsed, commit=args.commit,
                                             accept_buyer_mismatch=args.accept_buyer_mismatch)
        if args.json:
            return _out([{**r, "parsed": p.view()} for r, p in zip(results, parsed)])
        for r, p in zip(results, parsed):
            v = p.view()
            print(f"[{STATUS_TEXT[r['status']]}] {r['invoice_id'] or '（无号码）'}  {v['issue_date'] or '—'}  "
                  f"{v['total'] or '—':>10}  {v['seller_name'] or '—'}  ← {r['file']}")
            for x in r["issues"]:
                print(f"    ✕ {x}")
            for x in r["notes"]:
                print(f"    · {x}")
        if not args.commit and any(r["status"] == "would_import" for r in results):
            print("\n以上为预览，未写入。确认无误后加 --commit 导入。")
        return
    if args.action == "invoices":
        sql, a = "SELECT * FROM invoices WHERE company_id=?", [args.company]
        if args.month:
            sql, a = sql + " AND substr(issue_date,1,7)=?", a + [args.month]
        for r in conn.execute(sql + " ORDER BY issue_date, invoice_id", a):
            print(f"{r['company_id']} {r['invoice_id']} {r['issue_date']} {fmt(r['amount_cents']):>9}  "
                  f"用途={r['purpose'] or '（空）'}  备注={r['remark'] or '（空）'}  v{r['version']}")
    elif args.action == "bank":
        sql, a = "SELECT * FROM bank_txns WHERE company_id=?", [args.company]
        if args.month:
            sql, a = sql + " AND substr(txn_date,1,7)=?", a + [args.month]
        for r in conn.execute(sql + " ORDER BY txn_date, txn_id", a):
            print(f"{r['company_id']} {r['txn_id']} {r['txn_date']} {'收入' if r['direction'] == 'income' else '支出'} "
                  f"{fmt(r['amount_cents']):>9}  {r['counterparty']}  {r['note'] or ''}")
    elif args.action == "ops":
        for r in conn.execute("SELECT * FROM operations ORDER BY created_at"):
            print(f"{r['op_id']} {r['created_at']} {r['user_id']} {r['company_id']}/{r['invoice_id']} {r['status']}\n"
                  f"   前 {r['before_json']}\n   后 {r['after_json']}")


def cmd_serve(args, s):
    import uvicorn
    from .web import create_app
    uvicorn.run(create_app(s), host=args.host, port=args.port)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    s = load_settings()
    p = argparse.ArgumentParser(prog="python -m fiscal_harness", description="财税 Agent Harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("init", help="导入示例数据与业务规则")
    a.add_argument("--reset", action="store_true")
    a.set_defaults(func=cmd_init)

    for name, fn in (("chat", cmd_chat), ("ask", cmd_ask)):
        a = sub.add_parser(name, help="交互对话" if name == "chat" else "单轮提问（脚本与评测用）")
        a.add_argument("--user", default="U001")
        a.add_argument("--company", default="C001")
        a.add_argument("--session")
        if name == "ask":
            a.add_argument("message")
            a.add_argument("--json", action="store_true")
        a.set_defaults(func=fn)

    a = sub.add_parser("rules", help="规则知识库：list/show/search/resolve/import/update/disable/enable")
    a.add_argument("action", choices=["list", "show", "search", "resolve", "import", "update", "disable", "enable"])
    a.add_argument("rule_id", nargs="?")
    a.add_argument("version", nargs="?")
    a.add_argument("--company", default="C001")
    a.add_argument("--query", "-q")
    a.add_argument("--month")
    a.add_argument("--as-of")
    a.add_argument("--file")
    a.add_argument("--body-file")
    a.add_argument("--name")
    a.add_argument("--companies")
    a.add_argument("--effective-from")
    a.add_argument("--param", action="append", help="如 purpose_required_min_cents=50000")
    a.set_defaults(func=cmd_rules)

    a = sub.add_parser("memory", help="记忆：list/edit/delete")
    a.add_argument("action", choices=["list", "edit", "delete"])
    a.add_argument("memory_id", nargs="?")
    a.add_argument("--user", default="U001")
    a.add_argument("--company", default="C001")
    a.add_argument("--content")
    a.set_defaults(func=cmd_memory)

    a = sub.add_parser("runs", help="运行记录：list / show <run_id> / export [--session S | --all] --out DIR")
    a.add_argument("action", choices=["list", "show", "export"])
    a.add_argument("run_id", nargs="*")
    a.add_argument("--session")
    a.add_argument("--all", action="store_true")
    a.add_argument("--out", default="runs-export")
    a.add_argument("--limit", type=int, default=20)
    a.add_argument("--full", action="store_true")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_runs)

    a = sub.add_parser("data", help="核对当前保存的数据：invoices / bank / ops；导入发票文件：import-files")
    a.add_argument("action", choices=["invoices", "bank", "ops", "import-files"])
    a.add_argument("--files", nargs="+", help="import-files：PDF/OFD 文件或目录")
    a.add_argument("--company", default="C001")
    a.add_argument("--month")
    a.add_argument("--commit", action="store_true", help="import-files：确认写入（默认只预览）")
    a.add_argument("--accept-buyer-mismatch", action="store_true", help="import-files：购买方不是本企业时也导入（测试用）")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_data)

    a = sub.add_parser("recover", help="核对中断留下的未回读写入")
    a.set_defaults(func=cmd_recover)

    a = sub.add_parser("serve", help="启动演示网页")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8765)
    a.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args, s)


if __name__ == "__main__":
    main()
