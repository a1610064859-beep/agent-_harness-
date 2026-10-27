"""命令行入口。

    python -m invoicegen random -n 10 --seed 42 -o out/
    python -m invoicegen render examples/sample.json -o out/
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import date
from pathlib import Path

from .fake_data import fake_invoice, fake_tax_id
from .models import DEFECTS, Invoice, Party
from .render_pdf import render

KINDS = {"normal": "增值税普通发票", "special": "增值税专用发票"}


def _write(inv: Invoice, out: Path, formats: set[str]) -> dict:
    data = inv.to_dict()
    if "pdf" in formats:
        render(inv, out / f"{inv.number}.pdf")
    if "json" in formats:
        (out / f"{inv.number}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _write_index(rows: list[dict], out: Path):
    fields = ["number", "kind", "issue_date", "buyer", "seller",
              "total_amount", "total_tax", "grand_total", "defect"]
    with open(out / "index.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in rows:
            w.writerow({**{k: d[k] for k in fields}, "buyer": d["buyer"]["name"],
                        "seller": d["seller"]["name"]})


def write_batch(invoices: list[Invoice], out: Path, formats=("pdf", "json")) -> list[dict]:
    """写出每张发票的 PDF/JSON，再写汇总 index.csv。"""
    out.mkdir(parents=True, exist_ok=True)
    rows = [_write(inv, out, set(formats)) for inv in invoices]
    _write_index(rows, out)
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="invoicegen", description="测试用模拟发票生成器（输出均带测试水印，不可用于报销）")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("random", help="批量生成随机虚构发票")
    r.add_argument("-n", "--count", type=int, default=5)
    r.add_argument("--seed", type=int, help="随机种子，固定后结果可复现")
    r.add_argument("--kind", choices=KINDS, help="发票种类，默认随机")
    r.add_argument("--date", type=date.fromisoformat, help="开票日期上限 YYYY-MM-DD，默认今天")
    r.add_argument("--days", type=int, default=90, help="开票日期在上限往前多少天内，默认 90")
    r.add_argument("--buyer-name", help="指定购买方名称（如对接系统里的企业），默认随机虚构")
    r.add_argument("--buyer-tax-id", help="指定购买方税号，默认随机 TEST 开头")
    r.add_argument("--defect", choices=DEFECTS, help="故意印错票面，测下游校验：" +
                   "；".join(f"{k}={v}" for k, v in DEFECTS.items()))

    j = sub.add_parser("render", help="从 JSON 文件渲染指定内容的发票")
    j.add_argument("files", nargs="+", type=Path)

    for sp in (r, j):
        sp.add_argument("-o", "--out", type=Path, default=Path("out"))
        sp.add_argument("--format", default="pdf,json", help="pdf,json 任选，逗号分隔")

    args = p.parse_args(argv)
    formats = set(args.format.split(","))
    if not formats <= {"pdf", "json"}:
        p.error("--format 只支持 pdf、json")

    if args.cmd == "random":
        rng = random.Random(args.seed)
        kind = KINDS.get(args.kind)
        buyer = args.buyer_name and Party(name=args.buyer_name, tax_id=args.buyer_tax_id or fake_tax_id(rng))
        invoices = [fake_invoice(rng, i + 1, args.date, kind, buyer, args.days, args.defect or "")
                    for i in range(args.count)]
    else:
        invoices = []
        for f in args.files:
            raw = json.loads(f.read_text(encoding="utf-8"))
            invoices += [Invoice.from_dict(d) for d in (raw if isinstance(raw, list) else [raw])]

    rows = write_batch(invoices, args.out, formats)
    print(f"已生成 {len(rows)} 张测试发票 -> {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
