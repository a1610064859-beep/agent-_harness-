"""把《示例数据》Excel 导入 SQLite。导入后以库内当前数据为准，Excel 只作为初始资料。"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import openpyxl

from . import knowledge
from .money import to_cents

LEVELS = {"可查看并修改": "write", "仅查看": "read", "无权访问": "none"}
BUSINESS_TABLES = ("operations", "open_items", "tasks", "memories", "messages", "sessions", "run_steps", "runs",
                   "rule_versions", "invoice_sources", "invoices", "bank_txns", "permissions", "users", "companies")


def _rows_after_header(ws, first_header: str):
    """跳过说明行，从表头下一行开始读取，遇到空行或说明行结束。"""
    rows = ws.iter_rows(values_only=True)
    for row in rows:
        if row and row[0] == first_header:
            header = [c for c in row if c is not None]
            break
    else:
        raise ValueError(f"工作表 {ws.title} 中找不到表头“{first_header}”")
    for row in rows:
        if not row or row[0] is None:
            break
        if re.fullmatch(r"C\d{3}", str(row[0])):
            yield dict(zip(header, row))


def _date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def _text(value) -> str | None:
    """空单元格保持为空，不用“未填”之类文字占位。"""
    if value is None:
        return None
    return str(value)


def load_workbook_rows(path: Path) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["企业与用户"]
    companies, users, perms = [], [], []
    mode = None
    for row in ws.iter_rows(values_only=True):
        first = row[0] if row else None
        if first == "企业编号":
            mode = "company"
            continue
        if first == "用户编号":
            mode = "user"
            cols = [str(c) for c in row]
            continue
        if not first:
            if mode == "user":
                mode = None
            continue
        if mode == "company" and str(first).startswith("C"):
            companies.append({"company_id": first, "name": row[1], "short_name": row[2]})
        elif mode == "user" and str(first).startswith("U"):
            users.append({"user_id": first, "name": row[1]})
            for col, cell in zip(cols[2:], row[2:]):
                if cell is None:
                    continue
                cid = col[col.index("C"):col.index("C") + 4]
                perms.append({"user_id": first, "company_id": cid, "level": LEVELS[str(cell).strip()]})
    invoices = [{"company_id": r["企业编号"], "invoice_id": str(r["发票编号"]), "issue_date": _date(r["开票日期"]),
                 "seller": r["销售方名称"], "item": r["项目名称（票面）"], "amount_cents": to_cents(r["价税合计（元）"]),
                 "purpose": _text(r["用途"]), "remark": _text(r["备注"])}
                for r in _rows_after_header(wb["发票"], "企业编号")]
    bank = [{"company_id": r["企业编号"], "txn_id": str(r["流水编号"]), "txn_date": _date(r["交易日期"]),
             "direction": {"收入": "income", "支出": "expense"}[r["收支方向"]],
             "amount_cents": to_cents(r["金额（元）"]), "counterparty": r["对方名称"], "note": _text(r["备注"])}
            for r in _rows_after_header(wb["银行流水"], "企业编号")]
    return {"companies": companies, "users": users, "permissions": perms, "invoices": invoices, "bank_txns": bank}


def reset_and_import(conn: sqlite3.Connection, xlsx: Path, rules_docx: Path) -> dict:
    data = load_workbook_rows(xlsx)
    conn.execute("BEGIN")
    try:
        for table in BUSINESS_TABLES:
            conn.execute(f"DELETE FROM {table}")
        for table in ("companies", "users", "permissions", "invoices", "bank_txns"):
            for row in data[table]:
                cols = ", ".join(row)
                conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                             tuple(row.values()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    rules = knowledge.import_file(conn, rules_docx)
    return {k: len(v) for k, v in data.items()} | {"rule_versions": len(rules)}
