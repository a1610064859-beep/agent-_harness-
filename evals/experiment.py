"""对比实验：规则适用性由程序解析（baseline） vs 把全部规则版本原样交给模型自行判断（model_rules）。

python -m evals.experiment [--repeat 5] [--workers 8]

同一数据、同一模型、同一提问，只改变规则选择方式。为便于客观比较，提问要求回复最后单独一行
“缺用途：编号,编号”，由程序解析后与标准答案比对。结果写入 evals/results/experiment-<时间>/。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fiscal_harness import db, harness, importer, knowledge, runlog
from fiscal_harness.config import ROOT, load_settings
from fiscal_harness.llm import from_settings

ASK = "9月有哪些发票按规则必须填用途但还没填？回答最后单独一行用“缺用途：编号,编号”列出（没有就写“缺用途：无”）。"


def _fill(conn, company, items):
    for inv, purpose in items:
        conn.execute("UPDATE invoices SET purpose=? WHERE company_id=? AND invoice_id=?", (purpose, company, inv))


EXPERIMENTS = [
    ("X1", "甲企业：已停用的 1.0 版不能用（每张必填）", "C001", None,
     {"F002", "F003", "F004", "F006", "F009"}),
    ("X2", "乙企业：1000.00 元边界含等于，低于门槛的空白不算", "C002", None,
     {"F002", "F005", "F006"}),
    ("X3", "乙企业：规则新增 1.1 版（门槛 500 元，9 月起生效）后", "C002",
     lambda c: knowledge.add_version(c, "R003", "1.1", "单张发票金额大于或等于 500.00 元时必须填写用途，等于 500 元也在范围内。"
                                                       "空白或只有空格视为未填写，备注不能代替用途。", effective_from="2026-09-01"),
     {"F001", "F002", "F004", "F005", "F006"}),
    ("X4", "甲企业：已补 F002、F004，另有一张用途只有空格", "C001",
     lambda c: _fill(c, "C001", [("F002", "打印复印纸"), ("F004", "客户接待住宿"), ("F007", "   ")]),
     {"F003", "F006", "F007", "F009"}),
    ("X5", "乙企业：新增 10 月才生效的 2.0 版（门槛 2000 元），9 月仍用 1.0 版", "C002",
     lambda c: knowledge.add_version(c, "R003", "2.0", "自 2026 年 10 月起，单张发票金额大于或等于 2000.00 元时必须填写用途。"
                                                       "空白或只有空格视为未填写。", effective_from="2026-10-01"),
     {"F002", "F005", "F006"}),
    ("X6", "乙企业：新增 1.1 版（门槛 500 元）后又停用，应回到 1.0 版", "C002",
     lambda c: (knowledge.add_version(c, "R003", "1.1", "单张发票金额大于或等于 500.00 元时必须填写用途。",
                                      effective_from="2026-09-01"), knowledge.set_status(c, "R003", "1.1", "disabled")),
     {"F002", "F005", "F006"}),
    # 条文冲突题：1.0 版正文写明 9 月应使用 2.0 版，而 2.0 版被停用。
    # 按 1.0 版门槛给出 F006，或明确指出冲突、请用户确认，都算可接受；按已停用的 2.0 版作答算错误。
    ("X7", "甲企业：2.0 版停用、1.0 版恢复（条文互相矛盾）", "C001",
     lambda c: (knowledge.set_status(c, "R002", "2.0", "disabled"), knowledge.set_status(c, "R002", "1.0", "active")),
     {"F006"}),
]


FLAG_OK = {"X7"}  # 这些题中“指出冲突并请用户确认”也算可接受


def parse(reply: str) -> set[str] | None:
    m = None
    for m in re.finditer(r"缺用途[:：]\s*([^\n]*)", reply):
        pass
    if not m:
        return None
    return set(re.findall(r"F\d{3}", m.group(1)))


def run_one(exp, variant: str, attempt: int, settings, llm) -> dict:
    xid, title, company, setup, gold = exp
    s = dataclasses.replace(settings, variant=variant)
    conn = db.connect(":memory:")
    importer.reset_and_import(conn, s.sample_xlsx, s.rules_docx)
    if setup:
        setup(conn)
    sid = harness.start_session(conn, "U001", company)
    started = time.monotonic()
    r = harness.run_turn(conn, llm, s, sid, ASK)
    seconds = round(time.monotonic() - started, 2)
    got = parse(r.reply)
    usage = runlog.load(conn, r.run_id)["usage"]
    flagged = (not got) and bool(re.search(r"无法|待确认|不能确定|无法确定|冲突", r.reply.splitlines()[-1] if r.reply else ""))
    outcome = "exact" if got == gold else ("flagged" if flagged and xid in FLAG_OK else "wrong")
    return {"id": xid, "title": title, "variant": variant, "attempt": attempt, "gold": sorted(gold),
            "got": sorted(got) if got is not None else None, "exact": got == gold, "outcome": outcome,
            "false_positive": sorted(got - gold) if got is not None else None,
            "false_negative": sorted(gold - got) if got is not None else None,
            "tool_calls": len(r.tool_calls), "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0), "seconds": seconds, "reply": r.reply}


def _cost(rows) -> float | None:
    """按 FH_PRICE_IN / FH_PRICE_OUT（元/百万 tokens，取自所用平台的价目）估算每次平均费用；没设置就不估。"""
    try:
        price_in, price_out = float(os.environ["FH_PRICE_IN"]), float(os.environ["FH_PRICE_OUT"])
    except (KeyError, ValueError):
        return None
    total = sum(r["prompt_tokens"] * price_in + r["completion_tokens"] * price_out for r in rows) / 1e6
    return round(total / len(rows), 5)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--only", help="如 X5,X6")
    args = p.parse_args(argv)
    settings = load_settings()
    llm = from_settings(settings)
    exps = [e for e in EXPERIMENTS if not args.only or e[0] in args.only.split(",")]
    jobs = [(e, v, k + 1) for v in ("baseline", "model_rules") for e in exps for k in range(args.repeat)]
    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(lambda j: run_one(j[0], j[1], j[2], settings, llm), jobs))
    out = ROOT / "evals" / "results" / ("experiment-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True)
    summary = {"model": llm.model, "repeat": args.repeat, "question": ASK, "variants": {}}
    for v in ("baseline", "model_rules"):
        vs = [r for r in rows if r["variant"] == v]
        summary["variants"][v] = {
            "exact": f"{sum(r['exact'] for r in vs)}/{len(vs)}",
            "acceptable": f"{sum(r['outcome'] != 'wrong' for r in vs)}/{len(vs)}",
            "outcomes": {e[0]: {o: sum(r['outcome'] == o for r in vs if r['id'] == e[0]) for o in ("exact", "flagged", "wrong")}
                         for e in exps},
            "by_case": {e[0]: f"{sum(r['exact'] for r in vs if r['id'] == e[0])}/{args.repeat}" for e in exps},
            "unparsed": sum(r["got"] is None for r in vs),
            "false_positive_items": sum(len(r["false_positive"] or []) for r in vs),
            "false_negative_items": sum(len(r["false_negative"] or []) for r in vs),
            "avg_tool_calls": round(sum(r["tool_calls"] for r in vs) / len(vs), 2),
            "avg_prompt_tokens": round(sum(r["prompt_tokens"] for r in vs) / len(vs)),
            "avg_completion_tokens": round(sum(r["completion_tokens"] for r in vs) / len(vs)),
            # 单轮从提问到回复的墙钟时间；并发运行，含排队与网络波动，只宜在同一次实验内比较
            "avg_seconds": round(sum(r["seconds"] for r in vs) / len(vs), 2),
            "avg_cost_yuan": _cost(vs),
        }
    (out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in rows:
        if r["outcome"] != "exact":
            print(f"[{r['outcome'].upper()}] {r['variant']} {r['id']}#{r['attempt']} got={r['got']} fp={r['false_positive']} fn={r['false_negative']}")
    print(f"明细：{out}")


if __name__ == "__main__":
    sys.exit(main())
