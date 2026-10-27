"""扫描件与图片发票：识别结果的解析、校验、二维码核对与人工确认。MinerU 输出按实测结构构造，企业与税号均为虚构，
识别函数用替身，不需要安装 MinerU。"""
import io
import json
import sys
import textwrap

import pytest
from PIL import Image

from fiscal_harness import invoice_files as inf, ocr

# MinerU（basic 档）实测输出的结构：标签与值挤在同一格，竖排“购买方信息”混进名称，金额行与大写在同一行
OCR_DIGITAL = """## 电子发票（增值税专用发票）

发票号码:26312000000000000001

开票日期:2026年09月15日

<table><tr><td rowspan="2">购买方信 名称:甲企业 息</td><td colspan="4"></td><td rowspan="2">销售方信息</td><td colspan="3">名称:测试广告有限公司</td></tr><tr><td colspan="4">统一社会信用代码/纳税人识别号:</td><td colspan="3">统一社会信用代码/纳税人识别号:91310101MA1CTEN02Y</td></tr><tr><td>项目名称 *文化服务*广告发布费</td><td></td><td>规格型号</td><td>单位 项</td><td>数量 1</td><td>单价 1595.50</td><td>金额 1595.50</td><td>税率/征收率 6%</td><td>税额 95.73</td></tr><tr><td>合计</td><td></td></tr><tr><td>价税合计（大写）</td><td></td><td>壹仟陆佰玖拾壹圆贰角叁分</td><td></td><td></td><td>¥1595.50</td><td></td><td></td><td>¥95.73</td></tr><tr><td>合同号 HT001</td><td></td><td></td><td></td><td></td><td></td><td></td><td>(小写)¥1691.23</td><td></td></tr></table>

开票人:测试员
"""

# 旧版电子普票：购买方、销售方各一格，名称与税号连写；印章区识别文字放在 <details> 里
OCR_LEGACY = """## 测试增值税电子普通发票

<details>
<summary>seal</summary>

国家税务总局
</details>

发票代码:012345678901

发票号码:12345678

开票日期:2026年09月20日

<table><tr><td>购买方</td><td colspan="5">名         称:甲企业纳税人识别号:91330100MA2CTEN01X地址、电话:测试地址</td><td>密码区</td><td colspan="4">03392/&lt;9405&lt;-48</td></tr><tr><td colspan="2">货物或应税劳务、服务名称*信息技术服务*软件服务合      计</td><td>规格型号无</td><td>单位次</td><td>数量1</td><td colspan="3">单价100.00</td><td>金额100.00￥100.00</td><td>税率13%</td><td>税额13.00￥13.00</td></tr><tr><td colspan="2">价税合计（大写）</td><td colspan="9">⊗壹佰壹拾叁元整     (小写）￥113.00</td></tr><tr><td>销售方</td><td colspan="5">名         称:测试服务有限公司纳税人识别号:91440300MA2CTEN03W地址、电话:测试地址二</td><td>备注</td><td colspan="4"></td></tr></table>
"""


def png(qr_text: str | None = None) -> bytes:
    """一张图片：可带一个二维码。"""
    img = Image.new("L", (400, 300), 255)
    if qr_text:
        zxingcpp = pytest.importorskip("zxingcpp")
        z = zxingcpp.create_barcode(qr_text, zxingcpp.BarcodeFormat.QRCode).to_image(scale=4, add_quiet_zones=True)
        img.paste(Image.frombuffer("L", (z.shape[1], z.shape[0]), bytes(memoryview(z)), "raw", "L", 0, 1), (10, 10))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def fake(md):
    return lambda name, data: md


# ---------- 没开识别时 ----------

def test_image_and_scanned_pdf_without_ocr_explain_missing_text_layer():
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(buf, "PDF")
    for name, data in (("scan.png", png()), ("scan.pdf", buf.getvalue())):
        p = inf.parse_bytes(name, data)
        assert p.layout == "no_text" and p.issues == [inf.NO_TEXT], name


def test_text_layer_pdf_never_goes_to_ocr():
    def boom(name, data):
        raise AssertionError("有文字层的文件不应调用识别")
    p = inf.parse_bytes("x.pdf", b"%PDF-1.4 broken", ocr=boom)
    assert p.issues[0].startswith("文件无法解析")  # 走的是文字层解析


# ---------- 识别结果的解析与校验 ----------

def test_ocr_digital_layout():
    p = inf.parse_bytes("scan.png", png(), ocr=fake(OCR_DIGITAL))
    assert p.layout == inf.OCR_LAYOUT and p.issues == []
    assert (p.invoice_id, p.issue_date, p.invoice_type) == ("26312000000000000001", "2026-09-15", "数电专票")
    assert (p.buyer_name, p.buyer_tax_id) == ("甲企业", None)
    assert (p.seller_name, p.seller_tax_id) == ("测试广告有限公司", "91310101MA1CTEN02Y")
    assert (p.amount_cents, p.tax_cents, p.total_cents) == (159550, 9573, 169123)
    assert p.items == ["*文化服务*广告发布费"]
    assert any("人工核对" in n for n in p.notes)


def test_ocr_legacy_layout():
    p = inf.parse_bytes("scan.jpg", png(), ocr=fake(OCR_LEGACY))
    assert p.issues == [] and p.invoice_id == "012345678901-12345678"
    assert (p.buyer_name, p.seller_name) == ("甲企业", "测试服务有限公司")
    assert (p.buyer_tax_id, p.seller_tax_id) == ("91330100MA2CTEN01X", "91440300MA2CTEN03W")
    assert (p.amount_cents, p.tax_cents, p.total_cents) == (10000, 1300, 11300)
    assert p.items == ["*信息技术服务*软件服务"]  # 粘连的“合计”已去掉


@pytest.mark.parametrize("old,new,expect", [
    ("¥95.73</td></tr>", "¥95.78</td></tr>", "≠ 价税合计"),       # 税额认错一位
    ("(小写)¥1691.23", "(小写)¥1691.28", "大写金额"),             # 小写合计认错一位：两道核对都会拦
    ("(小写)¥1691.23", "1691.23", "未识别到价税合计"),           # ¥ 丢失
])
def test_ocr_misreads_are_rejected(old, new, expect):
    p = inf.parse_bytes("scan.png", png(), ocr=fake(OCR_DIGITAL.replace(old, new)))
    assert any(expect in x for x in p.issues), p.issues


def test_ocr_failure_becomes_issue():
    def broken(name, data):
        raise ocr.OcrError("Local parse-server is disabled.")
    p = inf.parse_bytes("scan.png", png(), ocr=broken)
    assert p.issues == ["识别失败：Local parse-server is disabled."]


def test_test_sample_watermark_is_caught_in_ocr(conn):
    md = OCR_DIGITAL.replace("开票人:测试员", "测试样张 · 非真实发票 · 不得报销抵扣")
    p = inf.parse_bytes("scan.png", png(), ocr=fake(md))
    assert p.is_test_sample
    assert inf.import_files(conn, "C001", [p])[0]["status"] == "rejected"


def test_uscc_check_digit():
    assert inf.uscc_ok("91330100MA2CTEN01X") is False  # 虚构税号：校验位不符
    assert inf.uscc_ok("91110000000000000Y") is False
    valid = "91110000000000000" + inf.USCC_CHARS[(31 - sum(inf.USCC_CHARS.index(c) * w for c, w in
                                                         zip("91110000000000000", inf.USCC_WEIGHTS)) % 31) % 31]
    assert inf.uscc_ok(valid)
    p = inf.parse_bytes("scan.png", png(), ocr=fake(OCR_DIGITAL))
    assert any("校验位不符" in n for n in p.notes)  # 只提示，不阻断（税号不参与业务计算）


# ---------- 二维码 ----------

def test_qr_fields():
    assert ocr.qr_fields("01,10,012345678901,12345678,100.00,20260920,1234,AB,") == {
        "type": "10", "code": "012345678901", "number": "12345678", "amount_cents": 10000, "issue_date": "2026-09-20"}
    assert ocr.qr_fields("01,32,,26312000000000000001,1691.23,20260915,,X,")["code"] is None
    assert ocr.qr_fields("hello") is None and ocr.qr_fields("01,10,a,b,not-money,2026") is None


def test_qr_matches_ocr():
    p = inf.parse_bytes("scan.png", png("01,10,012345678901,12345678,100.00,20260920,1234,AB,"), ocr=fake(OCR_LEGACY))
    assert p.issues == [] and "二维码核对一致：号码、日期、金额" in p.notes


@pytest.mark.parametrize("qr,expect", [
    ("01,10,012345678901,12345679,100.00,20260920,1234,AB,", "发票号码"),
    ("01,10,012345678901,12345678,100.00,20260921,1234,AB,", "开票日期"),
    ("01,10,012345678909,12345678,100.00,20260920,1234,AB,", "发票代码"),
    ("01,10,012345678901,12345678,108.00,20260920,1234,AB,", "金额"),
])
def test_qr_mismatch_blocks_import(qr, expect):
    p = inf.parse_bytes("scan.png", png(qr), ocr=fake(OCR_LEGACY))
    assert any("二维码" in x and expect in x for x in p.issues), p.issues


# ---------- 人工确认 ----------

def test_ocr_import_requires_confirmation(conn):
    p = inf.parse_bytes("scan.png", png(), ocr=fake(OCR_DIGITAL))
    r = inf.import_files(conn, "C001", [p])[0]
    assert r["status"] == "would_import" and any("须确认" in n for n in r["notes"])
    r = inf.import_files(conn, "C001", [p], commit=True)[0]
    assert r["status"] == "needs_confirmation"
    count = "SELECT COUNT(*) FROM invoices WHERE company_id='C001' AND invoice_id='26312000000000000001'"
    assert conn.execute(count).fetchone()[0] == 0
    r = inf.import_files(conn, "C001", [p], commit=True, confirm_ocr=True)[0]
    assert r["status"] == "imported" and conn.execute(count).fetchone()[0] == 1
    assert conn.execute("SELECT layout FROM invoice_sources WHERE invoice_id='26312000000000000001'").fetchone()[0] \
        == inf.OCR_LAYOUT


# ---------- 调用 MinerU 命令行 ----------

FAKE_MINERU = textwrap.dedent('''
    import json, pathlib, sys
    log = pathlib.Path("calls.jsonl")
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    state = pathlib.Path("started")
    cmd = sys.argv[1]
    if cmd == "server":
        state.write_text("1")
    elif cmd == "parse":
        if not state.exists():
            sys.stderr.write("Error: Local mineru server is not running. Run 'mineru server start'.")
            sys.exit(1)
        out = sys.argv[sys.argv.index("-o") + 1]
        pathlib.Path(out).write_text("发票号码:12345678", encoding="utf-8")
''')


def test_mineru_wrapper_starts_server_forgets_and_stays_local(tmp_path):
    script = tmp_path / "fake_mineru.py"
    script.write_text(FAKE_MINERU, encoding="utf-8")
    work = tmp_path / "ocr"
    run = ocr.mineru([sys.executable, str(script)], work, tier="standard", timeout=30)
    assert run("scan.png", png()) == "发票号码:12345678"
    calls = [json.loads(c) for c in (work / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c[0] for c in calls] == ["parse", "server", "parse", "forget"]
    assert all("--remote" not in c for c in calls) and calls[0][calls[0].index("--tier") + 1] == "standard"
    assert sorted(p.name for p in work.iterdir()) == ["calls.jsonl", "started"]  # 临时文件已清理


def test_mineru_wrapper_reports_errors(tmp_path):
    run = ocr.mineru([str(tmp_path / "no-such-mineru")], tmp_path / "ocr", timeout=5)
    with pytest.raises(ocr.OcrError):
        run("scan.png", png())
