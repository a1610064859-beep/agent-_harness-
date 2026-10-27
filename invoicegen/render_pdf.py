"""把 Invoice 渲染成 PDF。版面是简化的通用样式，满版水印，不含任何印章。"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .amount_words import to_rmb_upper
from .models import CENT, Invoice

FONT = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))

MAX_ROWS = 8
INK = colors.HexColor("#7a3b2e")
WATERMARK = "测试样张 · 非真实发票 · 不得报销抵扣"

# 明细列：(表头, 宽度mm, 对齐)
COLUMNS = [
    ("项目名称", 62, "l"), ("规格型号", 26, "l"), ("单位", 14, "c"), ("数量", 18, "r"),
    ("单价", 28, "r"), ("金额", 30, "r"), ("税率", 14, "c"), ("税额", 28, "r"),
]


def _fit(text: str, width: float, size: float) -> str:
    """超出宽度时截断并加省略号。"""
    if pdfmetrics.stringWidth(text, FONT, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + "…", FONT, size) > width:
        text = text[:-1]
    return text + "…"


def _cell(c, text, x, y, w, align, size=9):
    text = _fit(str(text), w - 3 * mm, size)
    c.setFont(FONT, size)
    if align == "r":
        c.drawRightString(x + w - 1.5 * mm, y, text)
    elif align == "c":
        c.drawCentredString(x + w / 2, y, text)
    else:
        c.drawString(x + 1.5 * mm, y, text)


def _fmt_qty(q):
    return f"{q.normalize():f}"


def _party_block(c, title, party, x, y_top, w, h):
    c.setStrokeColor(INK)
    c.rect(x, y_top - h, w, h)
    c.line(x + 8 * mm, y_top, x + 8 * mm, y_top - h)
    c.setFillColor(INK)
    c.setFont(FONT, 10)
    for i, ch in enumerate(title):
        c.drawCentredString(x + 4 * mm, y_top - 7 * mm - i * 5 * mm, ch)
    c.setFillColor(colors.black)
    rows = [("名称", party.name), ("纳税人识别号", party.tax_id),
            ("地址、电话", party.address_phone), ("开户行及账号", party.bank_account)]
    for i, (label, value) in enumerate(rows):
        yy = y_top - 6 * mm - i * 6 * mm
        c.setFont(FONT, 8.5)
        c.setFillColor(INK)
        c.drawString(x + 10 * mm, yy, label + "：")
        c.setFillColor(colors.black)
        _cell(c, value, x + 31 * mm, yy, w - 32 * mm, "l", 8.5)


def _watermark(c, page_w, page_h):
    c.saveState()
    c.setFillColor(colors.HexColor("#c0392b"))
    c.setFillAlpha(0.13)
    c.setFont(FONT, 26)
    c.translate(page_w / 2, page_h / 2)
    c.rotate(25)
    for row in range(-5, 6):
        for col in range(-2, 3):
            c.drawCentredString(col * 165 * mm + (row % 2) * 80 * mm, row * 28 * mm, WATERMARK)
    c.restoreState()


def render(inv: Invoice, path: Path) -> Path:
    if len(inv.items) > MAX_ROWS:
        raise ValueError(f"明细超过 {MAX_ROWS} 行，请拆分为多张测试发票")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    page_w, page_h = landscape(A4)
    c = canvas.Canvas(str(path), pagesize=(page_w, page_h))
    c.setTitle(f"测试发票 {inv.number}")
    c.setSubject("测试样张，非真实发票")

    table_w = sum(w for _, w, _ in COLUMNS) * mm
    x0 = (page_w - table_w) / 2
    y = page_h - 18 * mm

    # 标题
    c.setFillColor(INK)
    c.setFont(FONT, 20)
    c.drawCentredString(page_w / 2, y, f"测试用模拟{inv.kind}")
    c.setFont(FONT, 10)
    c.drawCentredString(page_w / 2, y - 7 * mm, "（非税务机关发票 · 仅供软件测试使用）")
    c.setStrokeColor(INK)
    c.line(page_w / 2 - 60 * mm, y - 2.5 * mm, page_w / 2 + 60 * mm, y - 2.5 * mm)
    c.setFillColor(colors.black)
    c.setFont(FONT, 9.5)
    c.drawRightString(x0 + table_w, y + 2 * mm, f"发票号码：{inv.number}")
    c.drawRightString(x0 + table_w, y - 4 * mm, f"开票日期：{inv.issue_date:%Y年%m月%d日}")

    # 买卖方
    y = page_h - 34 * mm
    half = table_w / 2
    party_h = 28 * mm
    _party_block(c, "购买方", inv.buyer, x0, y, half, party_h)
    _party_block(c, "销售方", inv.seller, x0 + half, y, half, party_h)
    y -= party_h

    # 明细表
    row_h = 7 * mm
    body_h = row_h * (MAX_ROWS + 1)
    c.rect(x0, y - body_h - row_h, table_w, body_h + row_h)
    xs = [x0]
    for _, w, _ in COLUMNS:
        xs.append(xs[-1] + w * mm)
    for xx in xs[1:-1]:
        c.line(xx, y, xx, y - body_h)
    c.setFillColor(INK)
    for (title, w, _), xx in zip(COLUMNS, xs):
        _cell(c, title, xx, y - 5 * mm, w * mm, "c")
    c.line(x0, y - row_h, x0 + table_w, y - row_h)
    c.setFillColor(colors.black)
    for r, item in enumerate(inv.items):
        yy = y - row_h * (r + 1) - 5 * mm
        values = [item.name, item.spec, item.unit, _fmt_qty(item.quantity),
                  f"{item.unit_price:,.2f}", f"{item.amount:,.2f}",
                  f"{item.tax_rate * 100:.0f}%", f"{item.tax:,.2f}"]
        for v, (_, w, align), xx in zip(values, COLUMNS, xs):
            _cell(c, v, xx, yy, w * mm, align)

    # 合计行
    y -= body_h
    c.line(x0, y, x0 + table_w, y)
    c.setFillColor(INK)
    _cell(c, "合　　计", xs[0], y - 5 * mm, COLUMNS[0][1] * mm, "c")
    c.setFillColor(colors.black)
    _cell(c, f"¥{inv.total_amount:,.2f}", xs[5], y - 5 * mm, COLUMNS[5][1] * mm, "r")
    _cell(c, f"¥{inv.total_tax:,.2f}", xs[7], y - 5 * mm, COLUMNS[7][1] * mm, "r")
    y -= row_h

    # 价税合计（缺陷样张在这里故意印错）
    shown_total = inv.grand_total + (CENT if inv.defect == "total" else 0)
    shown_upper = to_rmb_upper(shown_total + (1 if inv.defect == "upper" else 0))
    c.rect(x0, y - row_h, table_w, row_h)
    c.setFillColor(INK)
    _cell(c, "价税合计（大写）", x0, y - 5 * mm, 40 * mm, "l", 9.5)
    _cell(c, "（小写）", xs[5], y - 5 * mm, 20 * mm, "l", 9.5)
    c.setFillColor(colors.black)
    _cell(c, shown_upper, x0 + 36 * mm, y - 5 * mm, 140 * mm, "l", 10)
    _cell(c, f"¥{shown_total:,.2f}", xs[5] + 16 * mm, y - 5 * mm, 60 * mm, "l", 10)
    y -= row_h

    # 备注
    remark_h = 16 * mm
    c.rect(x0, y - remark_h, table_w, remark_h)
    c.line(x0 + 8 * mm, y, x0 + 8 * mm, y - remark_h)
    c.setFillColor(INK)
    c.setFont(FONT, 10)
    c.drawCentredString(x0 + 4 * mm, y - 6 * mm, "备")
    c.drawCentredString(x0 + 4 * mm, y - 12 * mm, "注")
    c.setFillColor(colors.black)
    _cell(c, inv.remark, x0 + 8 * mm, y - 6 * mm, table_w - 8 * mm, "l")
    y -= remark_h

    c.setFont(FONT, 9.5)
    c.drawString(x0, y - 7 * mm, f"开票人：{inv.issuer}")
    c.setFillColor(INK)
    c.drawRightString(x0 + table_w, y - 7 * mm, "本文件由测试工具自动生成，不具备任何票据效力")

    _watermark(c, page_w, page_h)
    c.showPage()
    c.save()
    return path
