"""用真实模型运行多轮评测：python -m evals.run [--repeat 3] [--only Q1,S5] [--workers 4]

每个案例在新导入的内存数据库上运行；结果写入 evals/results/<时间>/，包括逐例明细与汇总。
报告只写实际跑出的结果，失败项保留原因和运行编号以便追查。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fiscal_harness import db, harness, importer, runlog
from fiscal_harness.config import ROOT, load_settings
from fiscal_harness.llm import from_settings

from .cases import CASES, Case
from .imports import import_samples
from .uploads import build as build_uploads


@dataclass
class Ctx:
    conn: object
    results: list
    replies: list


def run_case(case: Case, settings, llm, attempt: int) -> dict:
    conn = db.connect(":memory:")
    importer.reset_and_import(conn, settings.sample_xlsx, settings.rules_docx)
    sid, user, company = None, case.user, case.company
    results, replies, transcript, usage = [], [], [], {"prompt_tokens": 0, "completion_tokens": 0}
    started = time.time()
    error = None
    try:
        for t in case.turns:
            if t.user:
                user, company = t.user, t.company or company
            if sid is None or t.new_session:
                if sid is not None:  # 新会话从上一会话结束时所在的企业开始
                    company = t.company or conn.execute("SELECT company_id FROM sessions WHERE session_id=?",
                                                        (sid,)).fetchone()[0]
                sid = harness.start_session(conn, user, company)
                if case.imports and len(replies) == 0:
                    import_samples(conn, sid, company, case.imports)
            r = harness.run_turn(conn, llm, settings, sid, t.text, attachments=build_uploads(t.attach) or None)
            u = runlog.load(conn, r.run_id)["usage"]
            for k in usage:
                usage[k] += u.get(k, 0)
            results.append(r)
            replies.append(r.reply)
            transcript.append({"session": sid, "user": user, "text": t.text, "attach": list(t.attach),
                               "reply": r.reply, "run_id": r.run_id,
                               "status": r.status, "tools": r.tool_calls, "anomalies": r.anomalies})
        ctx = Ctx(conn, results, replies)
        checks = [c(ctx) for c in case.checks]
    except Exception as e:  # 记录为失败，不中断其他案例
        error = f"{type(e).__name__}: {e}"
        checks = [(False, error)]
    failed = [msg for ok, msg in checks if not ok]
    return {"case_id": case.case_id, "category": case.category, "title": case.title, "attempt": attempt,
            "passed": not failed, "failed_checks": failed, "error": error,
            "anomalies": [a for t in transcript for a in t["anomalies"]],
            "usage": usage, "seconds": round(time.time() - started, 1), "transcript": transcript}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--only")
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    settings = load_settings()
    llm = from_settings(settings)
    cases = [c for c in CASES if not args.only or c.case_id in args.only.split(",")]
    jobs = [(c, k + 1) for k in range(args.repeat) for c in cases]
    out_dir = ROOT / "evals" / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(lambda j: run_case(j[0], settings, llm, j[1]), jobs))
    for r in rows:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {r['case_id']}#{r['attempt']} {r['title']}" + ("" if r["passed"] else f" → {r['failed_checks']}")
              + (f"  ⚠ 规则引用异常 {len(r['anomalies'])} 条" if r["anomalies"] else ""))
    passed = sum(r["passed"] for r in rows)
    # 异常（回复引用了本轮工具未返回的规则版本）单独统计，不计入通过条件；口径待定，见 docs/reviews
    anomalies = {f"{r['case_id']}#{r['attempt']}": len(r["anomalies"]) for r in rows if r["anomalies"]}
    usage = {k: sum(r["usage"][k] for r in rows) for k in ("prompt_tokens", "completion_tokens")}
    summary = {"model": llm.model, "today": settings.today.isoformat(), "runs": len(rows), "passed": passed,
               "cases": len(cases), "repeat": args.repeat, "usage": usage,
               "runs_with_anomalies": anomalies, "anomaly_total": sum(anomalies.values()),
               "by_case": {c.case_id: f"{sum(r['passed'] for r in rows if r['case_id'] == c.case_id)}/{args.repeat}"
                           for c in cases}}
    (out_dir / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n通过 {passed}/{len(rows)}（{len(cases)} 个案例 × {args.repeat} 次）｜模型 {llm.model}｜tokens {usage}")
    print(f"规则引用异常：{sum(anomalies.values())} 条，涉及 {len(anomalies)} 次运行（不计入通过条件）")
    print(f"明细：{out_dir}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
