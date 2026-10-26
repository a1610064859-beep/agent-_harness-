"""按 docs/demo-script.md 用真实模型走一遍演示，并导出关键运行记录：

python -m evals.demo_run [--out docs/acceptance/demo-runs]

使用独立的演示库 data/demo.db（每次重建），不影响 data/harness.db。
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from fiscal_harness import cli, db, harness, importer, knowledge
from fiscal_harness.config import ROOT, load_settings
from fiscal_harness.llm import from_settings

# (用户, 企业, 是否新会话, 消息)；消息为 None 时执行 action
SCRIPT = [
    ("U001", "C001", True, "发票总额是多少？"),
    (None, None, False, "9月的，顺便看下银行收支"),
    (None, None, False, "9月哪些发票还没填用途？"),
    (None, None, False, "F002用途写打印复印纸，F004是出差住宿，帮我保存"),
    (None, None, False, "F004说错了，应该是客户接待住宿"),
    (None, None, False, "记住：以后回答先给合计，再列明细"),
    ("U001", "C001", True, "还剩哪些没处理？"),
    (None, None, False, "切到乙企业，9月还缺哪些用途？"),
    ("U002", "C002", True, "乙企业9月发票总额多少？"),
    ("ACTION", None, False, "rule_update"),
    ("U001", "C002", True, "还剩哪些没处理？"),
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(ROOT / "docs" / "acceptance" / "demo-runs"))
    args = p.parse_args(argv)
    path = ROOT / "data" / "demo.db"
    for suffix in ("", "-wal", "-shm"):
        (path.parent / (path.name + suffix)).unlink(missing_ok=True)
    s = dataclasses.replace(load_settings(), db_path=path)
    conn = db.connect(path)
    importer.reset_and_import(conn, s.sample_xlsx, s.rules_docx)
    llm = from_settings(s)
    sid = None
    for user, company, new, text in SCRIPT:
        if user == "ACTION":
            knowledge.add_version(conn, "R003", "1.1", "单张发票金额大于或等于 500.00 元时必须填写用途，等于 500 元也在范围内。"
                                                       "空白或只有空格视为未填写，备注不能代替用途。", effective_from="2026-09-01")
            print("\n[操作] 规则管理：新增 R003 1.1 版（门槛 500 元，2026-09-01 起生效）")
            continue
        if new:
            sid = harness.start_session(conn, user, company)
            print(f"\n===== 新会话 {sid}（{user} / {company}）")
        r = harness.run_turn(conn, llm, s, sid, text)
        print(f"\n你> {text}\n助手> {r.reply}\n   ⤷ {r.run_id}｜{r.company_id}｜"
              f"{', '.join(c['tool'] + ('' if c['ok'] else '✗' + c['error']) for c in r.tool_calls) or '无工具'}")
    conn.close()
    out = Path(args.out)
    for stale in out.glob("R-*.json"):  # 只保留本次演示的记录
        stale.unlink()
    os.environ["FH_DB"] = str(path)  # 导出命令读取演示库
    cli.main(["runs", "export", "--all", "--out", args.out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
