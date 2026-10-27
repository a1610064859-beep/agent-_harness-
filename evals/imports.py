"""评测用的导入步骤：用 invoicegen 渲染固定内容的测试样张，走正式的解析、校验与导入，并在会话里记导入事件。

和操作员在网页上导入完全同一条路径，只是放行了测试样张（这些票号码以 TEST 开头，不是主办方数据）。
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from fiscal_harness import invoice_files
from invoicegen.cli import write_batch
from invoicegen.models import Invoice, LineItem, Party

# 金额刻意选在乙企业 1000.00 门槛两侧：A 价税合计 282.50，B 1590.00
SAMPLES = {
    "A": ("TEST20260918000001", "2026-09-18", "北辰星禾科技有限公司（测试）",
          LineItem("*纸制品*A4复印纸", 10, "25.00", "0.13", spec="70g 500张", unit="包")),
    "B": ("TEST20260925000002", "2026-09-25", "南湾远帆咨询有限公司（测试）",
          LineItem("*现代服务*咨询服务费", 1, "1500.00", "0.06", unit="次")),
}


def build(company_id: str, company_name: str, keys) -> list[Invoice]:
    buyer = Party(name=company_name, tax_id="TEST" + company_id.rjust(14, "0"))
    return [Invoice(number=no, issue_date=date.fromisoformat(day), buyer=buyer,
                    seller=Party(name=seller, tax_id="TEST" + no[-14:]), items=[item])
            for no, day, seller, item in (SAMPLES[k] for k in keys)]


def import_samples(conn, session_id: str, company_id: str, keys) -> str:
    """导入指定样张并在会话中记一条系统事件，返回事件文字。任何一张没导入成功都直接报错。"""
    name = conn.execute("SELECT name FROM companies WHERE company_id=?", (company_id,)).fetchone()["name"]
    invoices = build(company_id, name, keys)
    with tempfile.TemporaryDirectory() as d:
        write_batch(invoices, Path(d), formats=("pdf",))
        parsed = [invoice_files.parse_file(Path(d) / f"{inv.number}.pdf") for inv in invoices]
    results = invoice_files.import_files(conn, company_id, parsed, commit=True, allow_test_samples=True)
    bad = [r for r in results if r["status"] != "imported"]
    if bad:
        raise RuntimeError(f"评测前置导入失败：{bad}")
    return invoice_files.record_import_event(conn, session_id, company_id, results, parsed)
