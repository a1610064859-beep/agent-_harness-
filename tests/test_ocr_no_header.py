"""Regression coverage for an 8-digit No label on a VAT invoice header."""
import pytest

from fiscal_harness import invoice_files as inf
from .test_ocr import OCR_LEGACY, fake, png


@pytest.mark.parametrize("label", ["No 12345678", "NO.12345678", "no：12345678"])
def test_header_no_uses_existing_code_and_retains_confirmation(label, conn):
    p = inf.parse_bytes("scan.png", png(), ocr=fake(OCR_LEGACY.replace("发票号码:12345678", label)))
    assert p.invoice_id == "012345678901-12345678"
    assert not p.issues
    assert any("No" in note and "人工" in note for note in p.notes)
    assert inf.import_files(conn, "C001", [p], commit=True)[0]["status"] == "needs_confirmation"


def test_unlabelled_code_is_never_guessed_from_nearby_digits():
    md = OCR_LEGACY.replace("发票号码:12345678", "No 12345678").replace("发票代码:", "")
    p = inf.parse_bytes("scan.png", png(), ocr=fake(md))
    assert p.invoice_no == "12345678"
    assert p.invoice_code is None
    assert any("发票号码格式不符" in issue for issue in p.issues)


@pytest.mark.parametrize("replacement", [
    "订单 No:12345678",       # 不是独立的票头编号
    "No 123456789",          # 号码长度不符
    "No 12345678\nNo 87654321",  # 存在多个候选时不猜
    "No 12345678\n发票号码:无法识别",  # 明确的发票号码标签存在但读不出
])
def test_ambiguous_header_never_guesses_invoice_number(replacement):
    p = inf.parse_bytes("scan.png", png(), ocr=fake(OCR_LEGACY.replace("发票号码:12345678", replacement)))
    assert p.invoice_no is None
    assert "未识别到发票号码" in p.issues


def test_no_after_invoice_date_is_not_a_header_number():
    md = OCR_LEGACY.replace("发票号码:12345678", "").replace("开票日期:2026年09月20日", "开票日期:2026年09月20日\nNo 12345678")
    p = inf.parse_bytes("scan.png", png(), ocr=fake(md))
    assert p.invoice_no is None


def test_no_without_vat_title_is_not_used():
    md = OCR_LEGACY.replace("测试增值税电子普通发票", "其他单据").replace("发票号码:12345678", "No 12345678")
    p = inf.parse_bytes("scan.png", png(), ocr=fake(md))
    assert p.invoice_no is None


def test_no_header_still_cross_checks_qr():
    md = OCR_LEGACY.replace("发票号码:12345678", "No 12345678")
    qr = "01,10,012345678901,12345679,100.00,20260920,1234,AB,"
    p = inf.parse_bytes("scan.png", png(qr), ocr=fake(md))
    assert any("二维码" in issue and "发票号码" in issue for issue in p.issues)


def _paper_header():
    return (OCR_LEGACY.replace("测试增值税电子普通发票", "北京增值税普通发票")
            .replace("发票代码:012345678901", "012345678901")
            .replace("发票号码:12345678", "No 12345678"))


def test_paper_header_requires_independent_qr_and_confirmation(conn):
    qr = "01,10,012345678901,12345678,100.00,20260920,1234,AB,"
    p = inf.parse_bytes("paper.png", png(qr), ocr=fake(_paper_header()))
    assert p.invoice_id == "012345678901-12345678" and not p.issues
    assert any("代码与二维码" in note and "人工" in note for note in p.notes)
    assert inf.import_files(conn, "C001", [p], commit=True)[0]["status"] == "needs_confirmation"


@pytest.mark.parametrize("change", [
    lambda qr: qr.replace("012345678901", "012345678902"),
    lambda qr: qr.replace("12345678", "12345679"),
    lambda qr: qr.replace("100.00", "101.00"),
    lambda qr: qr.replace("20260920", "20260921"),
])
def test_paper_header_not_bound_if_qr_disagrees(change):
    qr = "01,10,012345678901,12345678,100.00,20260920,1234,AB,"
    p = inf.parse_bytes("paper.png", png(change(qr)), ocr=fake(_paper_header()))
    assert p.invoice_code is None
    assert p.issues


def test_paper_header_ambiguous_codes_not_bound():
    qr = "01,10,012345678901,12345678,100.00,20260920,1234,AB,"
    md = _paper_header().replace("012345678901\n", "012345678901\n012345678902\n")
    p = inf.parse_bytes("paper.png", png(qr), ocr=fake(md))
    assert p.invoice_code is None
    assert p.issues
