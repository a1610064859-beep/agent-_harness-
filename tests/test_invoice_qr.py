"""二维码读取、预览与 PDF 票面交叉核对；数据均为虚构。"""
import io

import pytest
from PIL import Image

from fiscal_harness import invoice_files as inf, ocr
from .test_invoice_files import DIGITAL
from .test_ocr import png


QR = "01,32,,26312000000000000001,1691.23,20260915,,X,"


def _pdf_with_qr(qr):
    zxingcpp = pytest.importorskip("zxingcpp")
    pytest.importorskip("pypdfium2")
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    barcode = zxingcpp.create_barcode(qr, zxingcpp.BarcodeFormat.QRCode).to_image(scale=4, add_quiet_zones=True)
    img = Image.frombuffer("L", (barcode.shape[1], barcode.shape[0]), bytes(memoryview(barcode)), "raw", "L", 0, 1)
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(300, 300))
    pdf.drawString(20, 280, "invoice text layer")
    pdf.drawImage(ImageReader(img), 30, 60, width=180, height=180)
    pdf.save()
    return buf.getvalue()


def test_image_qr_preview_without_ocr_does_not_allow_import(conn):
    p = inf.parse_bytes("invoice.png", png(QR))
    assert p.layout == "no_text" and p.issues == [inf.NO_TEXT]
    assert p.view()["qr_info"] == {"type": "32", "code": None, "number": "26312000000000000001",
                                   "issue_date": "2026-09-15", "amount": "1691.23"}
    assert any("仅凭二维码不能导入" in n for n in p.notes)
    assert inf.import_files(conn, "C001", [p], commit=True)[0]["status"] == "rejected"


@pytest.mark.parametrize("qr,expected_ok", [(QR, True), (QR.replace("00000000000001", "00000000000002"), False)])
def test_text_pdf_reads_qr_and_compares_with_extracted_text(monkeypatch, qr, expected_ok):
    parse_text = inf._parse_text
    monkeypatch.setattr(inf, "_parse_text", lambda inv, text: parse_text(inv, DIGITAL))
    p = inf.parse_bytes("invoice.pdf", _pdf_with_qr(qr))
    assert p.qr == qr
    if expected_ok:
        assert not p.issues and any("二维码核对一致" in n for n in p.notes)
    else:
        assert any("二维码" in issue and "发票号码" in issue for issue in p.issues)


def test_multiple_distinct_invoice_qrs_rejected():
    zxingcpp = pytest.importorskip("zxingcpp")
    out = Image.new("L", (900, 450), 255)
    for x, value in [(10, QR), (450, QR.replace("00000000000001", "00000000000002"))]:
        barcode = zxingcpp.create_barcode(value, zxingcpp.BarcodeFormat.QRCode).to_image(scale=4, add_quiet_zones=True)
        img = Image.frombuffer("L", (barcode.shape[1], barcode.shape[0]), bytes(memoryview(barcode)), "raw", "L", 0, 1)
        out.paste(img, (x, 10))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    with pytest.raises(ocr.QrConflictError):
        ocr.read_qr(buf.getvalue())
    parsed = inf.parse_bytes("invoice.png", buf.getvalue())
    assert any("多个内容不同" in issue for issue in parsed.issues)


@pytest.mark.parametrize("qr", [
    "01,32,,NOT_AN_INVOICE,100.00,20260920,,X,",
    "01,10,12345678901,12345678,100.00,20260920,,X,",
    "01,10,012345678901,12345678,100.00,202609201,,X,",
])
def test_malformed_qr_payload_is_not_invoice(qr):
    assert ocr.qr_fields(qr) is None


def test_non_invoice_qr_is_not_an_invoice():
    assert ocr.read_qr(png("https://example.com/invoice")) is None
