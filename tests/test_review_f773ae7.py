"""f773ae7 复评（E:\\claude\\inbox\\fiscal-agent-harness-f773ae7-review）提出的 2 项问题：评审探针转成回归测试。
数据均为虚构，识别函数用替身。"""
import pytest

from fiscal_harness import invoice_files as inf

from .test_invoice_files import KV
from .test_ocr import OCR_DIGITAL, OCR_LEGACY, fake, png

CHANGED_TAX = KV.replace("100.00|100.00|13%|13.00", "106.60|106.60|6%|6.40").replace("税额: 13.00", "税额: 6.40")


# ---------- 1. [P2] 同号票价税合计相同、金额与税额不同，应为冲突 ----------

@pytest.mark.parametrize("same_batch", [True, False])
def test_same_total_different_tax_breakdown_is_conflict(conn, same_batch):
    original, changed = inf.parse_text(KV, "first.pdf"), inf.parse_text(CHANGED_TAX, "changed-tax.pdf")
    assert original.issues == changed.issues == [] and original.total_cents == changed.total_cents
    if same_batch:
        r = inf.import_files(conn, "C001", [original, changed], commit=True)
    else:
        inf.import_files(conn, "C001", [original], commit=True)
        r = [None] + inf.import_files(conn, "C001", [changed], commit=True)
    assert r[1]["status"] == "conflict"
    assert "金额 100.00 → 106.60" in r[1]["issues"][0] and "税额 13.00 → 6.40" in r[1]["issues"][0]


def test_identical_reimport_is_still_duplicate(conn):
    inf.import_files(conn, "C001", [inf.parse_text(KV, "a.pdf")], commit=True)
    assert inf.import_files(conn, "C001", [inf.parse_text(KV, "b.pdf")], commit=True)[0]["status"] == "duplicate"


def test_sample_data_invoice_without_source_compares_table_fields(conn):
    """示例数据里的票没有来源文件，只能比较库表里的字段，并写明差在哪。"""
    existing = conn.execute("SELECT issue_date, seller, item, amount_cents FROM invoices "
                            "WHERE company_id='C001' AND invoice_id='F002'").fetchone()
    diff = inf._differences(conn, "C001", inf.parse_text(KV), existing)
    assert any(d.startswith("amount_cents 80000 → 11300") for d in diff)


# ---------- 2. [P2] 旧版票二维码金额只认不含税金额；数电票两种口径都收但提示未验证 ----------

def test_legacy_qr_with_tax_inclusive_amount_is_rejected(conn):
    p = inf.parse_bytes("scan.png", png("01,10,012345678901,12345678,113.00,20260920,1234,AB,"), ocr=fake(OCR_LEGACY))
    assert any("不含税金额 100.00 不一致" in x for x in p.issues)
    assert inf.import_files(conn, "C001", [p], commit=True, confirm_ocr=True)[0]["status"] == "rejected"


@pytest.mark.parametrize("amount", ["1595.50", "1691.23"])
def test_digital_qr_accepts_either_amount_with_unverified_note(amount):
    qr = f"01,32,,26312000000000000001,{amount},20260915,,X,"
    p = inf.parse_bytes("scan.png", png(qr), ocr=fake(OCR_DIGITAL))
    assert p.issues == [] and any("未经真实样本验证" in n for n in p.notes)


def test_digital_qr_with_unrelated_amount_is_rejected():
    p = inf.parse_bytes("scan.png", png("01,32,,26312000000000000001,1600.00,20260915,,X,"), ocr=fake(OCR_DIGITAL))
    assert any("都不一致" in x for x in p.issues)
