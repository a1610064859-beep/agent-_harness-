"""发票文件导入：OFD 与 PDF 文本层 → 标准字段 → 程序校验 → 入库。

只做确定性解析，不用 OCR，也不交给模型猜：字段取不到或校验对不上就列为问题，由人复核。
导入只在命令行由操作员执行；对话中的模型仍然只能改用途和备注。
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import ocr as ocr_mod
from .money import fmt, to_cents

SUFFIXES = (".pdf", ".ofd", *ocr_mod.IMAGE_SUFFIXES)
OCR_LAYOUT = "ocr_mineru"
USCC = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}$")
UPPER_CHARS = "负零壹贰叁肆伍陆柒捌玖拾佰仟万亿元圆角分整正"
MONEY = r"(-?[\d,]+\.\d{2})"


@dataclass
class ParsedInvoice:
    file_name: str
    sha256: str
    layout: str                      # ofd / pdf_digital / pdf_legacy / text_kv / test_sample / ocr_mineru / no_text / not_invoice
    invoice_type: str | None = None  # 数电专票 / 数电普票 / 增值税电子专用发票 / 增值税电子普通发票
    invoice_no: str | None = None
    invoice_code: str | None = None
    issue_date: str | None = None
    buyer_name: str | None = None
    buyer_tax_id: str | None = None
    seller_name: str | None = None
    seller_tax_id: str | None = None
    items: list[str] = field(default_factory=list)
    amount_cents: int | None = None  # 不含税合计
    tax_cents: int | None = None
    total_cents: int | None = None   # 价税合计，对应 invoices.amount_cents
    total_upper: str | None = None
    face_remark: str | None = None   # 票面备注，不写入 invoices.remark（那是用户填写的字段）
    red_of: str | None = None        # 红字发票对应的蓝字发票号码（取自票面备注）
    is_test_sample: bool = False     # invoicegen 生成的测试样张（TEST 号码、满版水印），默认不导入
    qr: str | None = None            # 票面二维码原文（识别件才读），用于核对号码、日期、金额
    doc_kind: str | None = None      # 认出不是增值税发票时的票据类别：receipt / fiscal_bill（见 NOT_INVOICE）
    issues: list[str] = field(default_factory=list)  # 阻断导入
    notes: list[str] = field(default_factory=list)   # 提示，不阻断

    @property
    def invoice_id(self) -> str | None:
        if not self.invoice_no:
            return None
        if self.is_test_sample or len(self.invoice_no) == 20:
            return self.invoice_no
        return f"{self.invoice_code}-{self.invoice_no}"

    @property
    def item_summary(self) -> str:
        if not self.items:
            return ""
        return self.items[0] if len(self.items) == 1 else f"{self.items[0]}等{len(self.items)}项"

    def view(self) -> dict:
        d = asdict(self)
        for k in ("amount_cents", "tax_cents", "total_cents"):
            v = d.pop(k)
            d[k.removesuffix("_cents")] = None if v is None else fmt(v)
        qr_fields = ocr_mod.qr_fields(self.qr) if self.qr else None
        d["qr_info"] = ({**{k: qr_fields[k] for k in ("type", "code", "number", "issue_date")},
                         "amount": fmt(qr_fields["amount_cents"])} if qr_fields else None)
        return {"invoice_id": self.invoice_id, **d}


# ---------- 大写金额 ----------

_DIG = dict(zip("零壹贰叁肆伍陆柒捌玖", range(10)))
_UNIT = {"拾": 10, "佰": 100, "仟": 1000}


def _section(text: str) -> int:
    total = num = 0
    for ch in text:
        if ch in _DIG:
            num = _DIG[ch]
        elif ch in _UNIT:
            total += (num or 1) * _UNIT[ch]
            num = 0
        else:
            raise ValueError(f"无法识别的大写字符“{ch}”")
    return total + num


def upper_to_cents(text: str) -> int:
    """把“壹仟陆佰玖拾壹圆贰角叁分”“负贰仟壹佰捌拾圆整”换算成分。"""
    s = text.strip().replace("（负数）", "负").replace("圆", "元")
    neg = s.startswith("负")
    s = s.lstrip("负").rstrip("整正")
    if not s:
        raise ValueError("大写金额为空")
    yuan, has_yuan, frac = s.partition("元")
    if not has_yuan:
        yuan, frac = "", s
    integer = 0
    for big, mult in (("亿", 10 ** 8), ("万", 10 ** 4)):
        if big in yuan:
            head, yuan = yuan.split(big, 1)
            integer += _section(head) * mult
    integer += _section(yuan)
    jiao = fen = 0
    if m := re.search(r"([零壹贰叁肆伍陆柒捌玖])角", frac):
        jiao = _DIG[m.group(1)]
    if m := re.search(r"([零壹贰叁肆伍陆柒捌玖])分", frac):
        fen = _DIG[m.group(1)]
    if re.sub(r"[零壹贰叁肆伍陆柒捌玖角分]", "", frac):
        raise ValueError(f"无法识别的角分部分“{frac}”")
    cents = integer * 100 + jiao * 10 + fen
    return -cents if neg else cents


# ---------- 文本解析（PDF 文本层，以及没有标签结构的 OFD） ----------

def _money(text: str | None) -> int | None:
    return None if text is None else to_cents(text.replace(",", ""))


def _first(pattern: str, text: str, flags=0) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _items(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        line = re.sub(r"\s+\*", "*", line.strip())  # “*文化服务 *广告发布费”
        if m := re.match(r"^(\*[一-鿿][^*\s|]*\*[^\s|]+)", line):  # 分类以汉字开头，排除密码区
            out.append(m.group(1))
    return out


def _date(text: str) -> str | None:
    if m := re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text):
        try:
            return date(*map(int, m.groups())).isoformat()
        except ValueError:
            return None
    return None


def _ocr_date(text: str) -> str | None:
    """识别件只在“开票日期”标签附近读取日期，避免误取其他业务日期。"""
    label = re.compile(r"开票日期[ \t]*[:：]?[ \t]*(?:\n[ \t]*)?")
    labels = list(label.finditer(text))
    if not labels:
        return _date(text)  # 兼容旧版无标签输出
    dates = set()
    for m in labels:
        value = text[m.end():].split("\n", 1)[0]
        match = re.match(
            r"(\d{4})(?:[ \t]*年[ \t]*|[ \t]*[-/.][ \t]*|[ \t]+)"
            r"(\d{1,2})(?:[ \t]*月[ \t]*|[ \t]*[-/.][ \t]*|[ \t]+)"
            r"(\d{1,2})(?:[ \t]*日)?(?!\d)", value)
        if match:
            try:
                dates.add(date(*map(int, match.groups())).isoformat())
            except ValueError:
                pass
    return next(iter(dates)) if len(dates) == 1 else None


TYPES = (("电子发票（增值税专用发票）", "数电专票"), ("电子发票（普通发票）", "数电普票"),
         ("增值税电子专用发票", "增值税电子专用发票"), ("增值税电子普通发票", "增值税电子普通发票"))


def _type(text: str) -> str | None:
    compact = re.sub(r"\s+", "", text).replace("(", "（").replace(")", "）")
    return next((name for title, name in TYPES if title in compact), None)


def _upper(text: str) -> str | None:
    for token in re.findall(f"[{UPPER_CHARS}]{{3,}}", text):
        if "元" in token or "圆" in token:
            return token
    return None


TEST_MARKS = ("测试样张", "测试用模拟")

# 常被当成发票拍照上传、但不是增值税发票的票据：认出来就只报这一条原因，不再逐项报“未识别到发票号码”
NOT_INVOICE = {
    "receipt": "这是购物小票（收银条），不是发票：请按小票上的提取码或二维码开具电子发票，再导入开出的发票文件",
    "fiscal_bill": "这是财政电子票据（如医疗门诊收费票据），不是增值税发票，目前不支持导入；"
                   "真伪可在全国财政电子票据查验平台 pjcy.mof.gov.cn 查验",
}
INVOICE_MARKS = re.compile(r"发票号码|发票代码|价税合计|纳税人识别号|开票日期|购买方|销售方")
RECEIPT_MARKS = re.compile(r"收银条|小票|提取码|申请电子发票|扫码开票|开具\S{0,10}发票")


def _doc_kind(text: str) -> str | None:
    """票面上有发票特有的栏目就按发票处理（漏识的号码等照常报问题），避免把真发票误判成小票拒收。
    财政票据也印“开票日期”，所以先按“票据代码”认它，再用其余发票栏目排除小票。"""
    if re.search(r"发票号码|发票代码", text):
        return None
    if "票据代码" in text and re.search(r"票据号码|交款人|收款单位", text):
        return "fiscal_bill"
    if not INVOICE_MARKS.search(text) and RECEIPT_MARKS.search(text):
        return "receipt"
    return None


def _parse_text(inv: ParsedInvoice, text: str) -> None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    inv.issue_date = _date(text)
    inv.invoice_type = _type(text)
    inv.items = _items(lines)
    inv.total_upper = _upper(text)
    if any(m in text for m in TEST_MARKS) or re.search(r"发票号码[:：]\s*TEST", text):
        inv.layout = "test_sample"
        _parse_test_sample(inv, text)
        return
    inv.doc_kind = _doc_kind(text)
    if inv.doc_kind:
        inv.layout = "not_invoice"
        return
    no = _first(r"发票号码[:：]\s*([0-9][0-9 ]{6,}[0-9])", text)
    if no:
        inv.invoice_no = no.replace(" ", "")
        inv.invoice_code = _first(r"发票代码[:：]\s*(\d+)", text)  # 位数在 _validate 统一核对
    if "统一社会信用代码" in text and inv.invoice_no:
        inv.layout = "pdf_digital"
        _parse_digital(inv, text, lines)
    elif "购买方名称" in text or "销售方名称" in text:
        inv.layout = "text_kv"
        _parse_kv(inv, text, lines)
    else:
        inv.layout = "pdf_legacy"
        _parse_legacy(inv, text, lines)


def _parse_digital(inv: ParsedInvoice, text: str, lines: list[str]) -> None:
    if m := re.search(r"名称[:：]\s*(.+?)\s+名称[:：]\s*(.+)", text):
        inv.buyer_name, inv.seller_name = m.group(1).strip(), m.group(2).strip()
    for line in lines:
        parts = re.split(r"统一社会信用代码\s*/\s*纳税人识别号[:：]", line)
        if len(parts) == 3:
            inv.buyer_tax_id, inv.seller_tax_id = (p.strip() or None for p in parts[1:])
            break
    if m := re.search(rf"合\s*计\s*[¥￥]\s*{MONEY}\s*[¥￥]\s*{MONEY}", text):
        inv.amount_cents, inv.tax_cents = _money(m.group(1)), _money(m.group(2))
    inv.total_cents = _money(_first(rf"（小写）\s*[¥￥]\s*{MONEY}", text))
    if m := re.search(r"\n备\s*\n注\s*\n(.*?)(?=^(?:开票人|收款人)|\Z)", text, re.S | re.M):
        inv.face_remark = m.group(1).strip() or None


def _parse_kv(inv: ParsedInvoice, text: str, lines: list[str]) -> None:
    inv.buyer_name = _first(r"购买方名称[:：]\s*(.+)", text)
    inv.seller_name = _first(r"销售方名称[:：]\s*(.+)", text)
    inv.buyer_tax_id = _first(r"购买方纳税人识别号[:：]\s*(\S+)", text)
    inv.seller_tax_id = _first(r"销售方纳税人识别号[:：]\s*(\S+)", text)
    inv.tax_cents = _money(_first(rf"^税额[:：]\s*[¥￥]?{MONEY}", text, re.M))
    inv.total_cents = _money(_first(rf"价税合计[:：]\s*[¥￥]?{MONEY}", text))
    header = next((ln.split("|") for ln in lines if ln.startswith("项目名称|")), None)
    if header and "金额" in header:
        col = header.index("金额")
        rows = [ln.split("|") for ln in lines if ln.startswith("*") and "|" in ln]
        if rows and all(len(r) > col for r in rows):
            inv.amount_cents = sum(_money(r[col]) for r in rows)


def _parse_test_sample(inv: ParsedInvoice, text: str) -> None:
    """invoicegen 生成的测试样张：标签与值可能分在两行，购买方、销售方按出现先后识别。"""
    inv.is_test_sample = True
    inv.invoice_no = _first(r"发票号码[:：]\s*(TEST\w+)", text)
    if kind := _first(r"测试用模拟(\S+发票)", text):
        inv.invoice_type = f"测试样张·{kind}"
    names = re.findall(r"名称[:：]\s*(.+)", text)
    tax_ids = re.findall(r"纳税人识别号[:：]\s*(\S+)", text)
    if len(names) >= 2:
        inv.buyer_name, inv.seller_name = names[0].strip(), names[1].strip()
    if len(tax_ids) >= 2:
        inv.buyer_tax_id, inv.seller_tax_id = tax_ids[:2]
    if m := re.search(rf"合\s*计\s*[¥￥]\s*{MONEY}\s*[¥￥]\s*{MONEY}", text):
        inv.amount_cents, inv.tax_cents = _money(m.group(1)), _money(m.group(2))
    inv.total_cents = _money(_first(rf"（小写）[\s\S]*?[¥￥]\s*{MONEY}", text))
    inv.face_remark = _first(r"^备\s*\n注\s*\n(.+)$", text, re.M)


def _parse_legacy(inv: ParsedInvoice, text: str, lines: list[str]) -> None:
    """2017–2020 年的增值税电子普通发票：标签与值在文本层中分开，按值的形态和先后顺序识别。"""
    for i, line in enumerate(lines):
        if re.fullmatch(r"\d{8}", line) and i and re.fullmatch(r"\d{10}|\d{12}", lines[i - 1]):
            inv.invoice_code, inv.invoice_no = lines[i - 1], line
            break
    parties = [(lines[i - 1], ln) for i, ln in enumerate(lines) if i and USCC.match(ln)]
    if len(parties) >= 2:
        (inv.buyer_name, inv.buyer_tax_id), (inv.seller_name, inv.seller_tax_id) = parties[:2]
        inv.notes.append("旧版版式：购买方、销售方按版面先后顺序识别")
    for line in lines:
        if m := re.fullmatch(rf"[¥￥]{MONEY}\s+[¥￥]{MONEY}", line):
            inv.amount_cents, inv.tax_cents = _money(m.group(1)), _money(m.group(2))
    singles = [m.group(1) for ln in lines if (m := re.fullmatch(rf"[¥￥]{MONEY}", ln))]
    if singles:
        inv.total_cents = _money(singles[-1])


# ---------- 识别件（MinerU 输出的 Markdown，表格为 HTML） ----------

def _flatten(md: str) -> str:
    """把 MinerU 的 HTML 表格和 Markdown 标记拆成逐格逐行的纯文字；“*类别*项目”里的星号保留。"""
    s = re.sub(r"<details>.*?</details>", "", md, flags=re.S)  # 印章区的识别文字，不是票面字段
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"</t[dh]>\s*|<br\s*/?>|</tr>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"^#+\s*", "", s, flags=re.M)


def _party_name(raw: str | None) -> str | None:
    """去掉竖排“购买方信息”等标签混进名称的残字，如“个人 息”“XX公司 购买方信息”。"""
    name = re.sub(r"\s+[购买销售方信息]{1,5}$", "", (raw or "").strip()).strip()
    return name or None


def _legacy_header_no(text: str) -> str | None:
    """旧版票面可能仅以 No 标注八位号码；仅使用明确的票头，不推断发票代码。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    titles = [i for i, line in enumerate(lines) if "增值税" in line and "发票" in line]
    if len(titles) != 1:
        return None
    candidates = [(i, match.group(1)) for i, line in enumerate(lines)
                  if (match := re.match(r"^No[ \t]*[.．:：]?[ \t]*([0-9]{8})(?![0-9])", line, re.I))]
    if len(candidates) != 1:
        return None
    title = titles[0]
    index, number = candidates[0]
    dates = [i for i, line in enumerate(lines) if i > title and "开票日期" in line]
    if not dates or not (title < index < dates[0]) or index - title > 12:
        return None
    return number


def _parse_ocr(inv: ParsedInvoice, md: str) -> None:
    """识别件：标签与值常挤在同一格，按标签取值。金额与税额按位置取（除小写合计外的前两个 ¥ 金额），
    不在数字里挑能凑平的组合，所以认错的数字会被后面的核对拦下。"""
    inv.layout = OCR_LAYOUT
    text = _flatten(md)
    if not any(m in text for m in TEST_MARKS) and (kind := _doc_kind(text)):
        inv.doc_kind = kind
        return
    inv.issue_date = _ocr_date(text)
    inv.invoice_type = _type(text)
    inv.total_upper = _upper(text)
    inv.items = [re.sub(r"合\s*计?$", "", m) for m in  # 旧版版面里项目名常和下一格的“合计”粘在一起
                 re.findall(r"(\*[一-鿿][^*\s|]*\*[^\s|]+)", text)]
    inv.invoice_no = _first(r"发票号码[:：]?[ \t]*(TEST\w+|\d{20}|\d{8})(?!\d)", text)
    if not inv.invoice_no and "发票号码" not in text and (number := _legacy_header_no(text)):
        inv.invoice_no = number
        inv.notes.append("票号由票头 No 标记识别，请人工对照票面核实")
    if any(m in text for m in TEST_MARKS):  # 测试样张的水印、标题即使号码认不出也要识别出来，默认拒收
        inv.is_test_sample = True
    inv.invoice_code = _first(r"发票代码[:：]?[ \t]*(\d{12}|\d{10})(?!\d)", text)
    names = re.findall(r"名\s*称[:：][ \t]*(.*?)(?=纳税人识别号|统一社会信用代码|\n|$)", text)
    if len(names) >= 2:
        inv.buyer_name, inv.seller_name = _party_name(names[0]), _party_name(names[1])
    tax_ids = re.findall(r"纳税人识别号[:：]?[ \t]*([0-9A-Z]{15,20})?", text)
    if len(tax_ids) >= 2:
        inv.buyer_tax_id, inv.seller_tax_id = (t or None for t in tax_ids[:2])
    total = re.search(rf"小写[)）]?\s*[¥￥]\s*{MONEY}", text)
    if total:
        inv.total_cents = _money(total.group(1))
        others = [m for m in re.finditer(rf"[¥￥]\s*{MONEY}", text) if not (total.start() <= m.start() < total.end())]
        if len(others) >= 2:
            inv.amount_cents, inv.tax_cents = _money(others[0].group(1)), _money(others[1].group(1))


USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def uscc_ok(code: str) -> bool:
    """统一社会信用代码校验位（GB 32100—2015）。"""
    if not USCC.match(code or ""):
        return False
    total = sum(USCC_CHARS.index(c) * w for c, w in zip(code, USCC_WEIGHTS))
    return code[-1] == USCC_CHARS[(31 - total % 31) % 31]


def _bind_paper_header_from_qr(inv: ParsedInvoice, q: dict, text: str) -> None:
    """仅在纸票票头、二维码、日期和不含税金额互相印证时补足无标签代码。"""
    header = text.split("开票日期", 1)[0]
    if ("开票日期" not in text or "增值税" not in header or "发票" not in header
            or "电子" in header or not q["code"] or not re.fullmatch(r"\d{10}|\d{12}", q["code"])
            or inv.issue_date != q["issue_date"] or inv.amount_cents is None
            or inv.amount_cents != q["amount_cents"]):
        return
    no_candidates = re.findall(r"(?<![A-Za-z])No[ \t]*[.．:：]?[ \t]*(\d{8})(?!\d)", header, re.I)
    if set(no_candidates) != {q["number"]}:
        return
    if inv.invoice_no and inv.invoice_no != q["number"]:
        return
    if inv.invoice_no is None and "发票号码" not in text:
        inv.invoice_no = q["number"]
        inv.notes.append("票号由票头 No 与二维码共同核对，请人工对照票面核实")
    candidates = set(re.findall(r"(?<!\d)(?:\d{12}|\d{10})(?!\d)", header))
    if (inv.invoice_no == q["number"] and inv.invoice_code is None and "发票代码" not in text
            and candidates == {q["code"]}):
        inv.invoice_code = q["code"]
        inv.notes.append("无标签票头代码与二维码共同核对，请人工对照票面核实")


def _check_ocr(inv: ParsedInvoice, data: bytes, text: str = "") -> None:
    """识别件的额外核对：税号校验位、票面二维码与识别结果是否一致。"""
    inv.notes.append("识别件：发票代码（如有）、号码、日期和名称须在导入前对照票面人工核对")
    for label, code in (("购买方", inv.buyer_tax_id), ("销售方", inv.seller_tax_id)):
        if code and len(code) == 18 and not code.startswith("TEST") and not uscc_ok(code):
            inv.notes.append(f"{label}税号 {code} 校验位不符，可能识别有误，请核对")
    _check_qr(inv, data, text=text, ocr=True)


def _read_qr(inv: ParsedInvoice, data: bytes) -> dict | None:
    try:
        inv.qr = ocr_mod.read_qr(data)
    except ocr_mod.QrConflictError as e:
        inv.issues.append(str(e))
        return None
    except ImportError:
        inv.notes.append("未安装二维码组件（requirements-ocr.txt），未读取二维码")
        return None
    except Exception as e:  # 图片损坏等：不阻断，交给人工核对
        inv.notes.append(f"二维码读取失败：{type(e).__name__}")
        return None
    q = inv.qr and ocr_mod.qr_fields(inv.qr)
    if not q:
        inv.notes.append("未读到有效的发票二维码，请人工核对票面")
    return q


def _check_qr(inv: ParsedInvoice, data: bytes, text: str = "", ocr: bool = False) -> None:
    """对 OCR 与 PDF 文字层的结果执行同一二维码核对。"""
    q = _read_qr(inv, data)
    if not q:
        return
    if ocr:
        _bind_paper_header_from_qr(inv, q, text)
    complete = True
    if inv.invoice_no is None:
        inv.notes.append("未识别到票面发票号码，无法与二维码核对")
        complete = False
    elif q["number"] != inv.invoice_no:
        inv.issues.append(f"二维码中的发票号码 {q['number']} 与识别结果 {inv.invoice_no} 不一致")
    if q["code"] and inv.invoice_code is None:
        inv.notes.append("未识别到票面发票代码，无法与二维码核对")
        complete = False
    elif q["code"] and q["code"] != inv.invoice_code:
        inv.issues.append(f"二维码中的发票代码 {q['code']} 与识别结果 {inv.invoice_code} 不一致")
    if inv.issue_date is None:
        inv.notes.append("未识别到票面开票日期，无法与二维码核对")
        complete = False
    elif q["issue_date"] != inv.issue_date:
        inv.issues.append(f"二维码中的开票日期 {q['issue_date']} 与识别结果 {inv.issue_date} 不一致")
    if len(q["number"]) == 20:
        # 数电票：没有取得带二维码的真实样本，金额口径未经验证，不含税金额或价税合计任一一致都接受
        if inv.amount_cents is None and inv.total_cents is None:
            inv.notes.append("未识别到票面金额和价税合计，无法与二维码核对")
            complete = False
        elif q["amount_cents"] not in (inv.amount_cents, inv.total_cents):
            inv.issues.append(f"二维码中的金额 {fmt(q['amount_cents'])} 与识别出的金额、价税合计都不一致")
        else:
            inv.notes.append("数电票二维码金额口径未经真实样本验证（不含税或价税合计任一一致即可），请人工核对金额")
    elif inv.amount_cents is None:
        inv.notes.append("未识别到票面不含税金额，无法与二维码核对")
        complete = False
    elif q["amount_cents"] != inv.amount_cents:  # 旧版票：二维码金额为不含税金额（真实样本实测）
        inv.issues.append(f"二维码中的金额 {fmt(q['amount_cents'])} 与识别出的不含税金额 "
                          f"{'—' if inv.amount_cents is None else fmt(inv.amount_cents)} 不一致")
    if complete and not any("二维码" in x for x in inv.issues):
        inv.notes.append("二维码核对一致：号码、日期、金额")


# ---------- OFD ----------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_ofd(inv: ParsedInvoice, z: zipfile.ZipFile) -> None:
    tag_files = [n for n in z.namelist() if n.endswith("CustomTag.xml")]
    if not tag_files:
        # 没有语义标签的 OFD：退回到按全部文字解析
        text = "\n".join(t.strip() for n in z.namelist() if n.endswith(".xml")
                         for t in ET.fromstring(z.read(n)).itertext() if t.strip())
        _parse_text(inv, text)
        inv.notes.append("OFD 中没有发票语义标签，按文字内容识别")
        return
    inv.layout = "ofd"
    texts: dict[str, str] = {}
    for n in z.namelist():
        if n.endswith("Content.xml"):
            for obj in ET.fromstring(z.read(n)).iter():
                if _local(obj.tag) == "TextObject":
                    texts[obj.get("ID")] = "".join(t for c in obj if _local(c.tag) == "TextCode" for t in [c.text or ""])
    tags: dict[str, list[str]] = {}
    for el in ET.fromstring(z.read(tag_files[0])).iter():
        refs = [r.text.strip() for r in el if _local(r.tag) == "ObjectRef" and r.text]
        if refs:
            tags.setdefault(_local(el.tag), []).append("".join(texts.get(r, "") for r in refs).strip())

    def one(name):
        return (tags.get(name) or [None])[0] or None

    def amount(name):
        v = one(name)
        return _money(re.sub(r"[¥￥\s]", "", v)) if v else None

    inv.invoice_no = (one("InvoiceNo") or "").replace(" ", "") or None
    inv.invoice_code = one("InvoiceCode")
    inv.issue_date = _date(one("IssueDate") or "")
    inv.buyer_name, inv.buyer_tax_id = one("BuyerName"), one("BuyerTaxID")
    inv.seller_name, inv.seller_tax_id = one("SellerName"), one("SellerTaxID")
    inv.items = [re.sub(r"\s+", "", t) for t in tags.get("Item", []) if t]
    inv.amount_cents = amount("TaxExclusiveTotalAmount")
    inv.tax_cents = amount("TaxTotalAmount")
    inv.total_cents = amount("TaxInclusiveTotalAmount")
    inv.face_remark = one("Note")
    inv.total_upper = _upper("".join(texts.values()))
    inv.invoice_type = _type("".join(texts.values()))  # 票名通常在模板页的文字里
    # OFD.xml 的元数据与版面文字互相印证
    meta = {}
    root = ET.fromstring(z.read("OFD.xml"))
    for el in root.iter():
        if _local(el.tag) == "CustomData":
            meta[el.get("Name")] = (el.text or "").strip()
    if meta.get("发票号码") and meta["发票号码"] != inv.invoice_no:
        inv.issues.append(f"OFD 元数据发票号码 {meta['发票号码']} 与票面 {inv.invoice_no} 不一致")
    for key, val in (("合计金额", inv.amount_cents), ("合计税额", inv.tax_cents)):
        if meta.get(key) and val is not None and _money(meta[key]) != val:
            inv.issues.append(f"OFD 元数据{key} {meta[key]} 与票面 {fmt(val)} 不一致")


# ---------- 入口：解析与校验 ----------

def _validate(inv: ParsedInvoice) -> None:
    if inv.doc_kind:
        inv.issues.append(NOT_INVOICE[inv.doc_kind])
        return
    for label, value in (("发票号码", inv.invoice_no), ("开票日期", inv.issue_date), ("销售方名称", inv.seller_name),
                         ("价税合计", inv.total_cents)):
        if value is None:
            inv.issues.append(f"未识别到{label}")
    if inv.invoice_no and inv.invoice_no.upper().startswith("TEST"):
        inv.is_test_sample = True  # 不管识别成哪种版式，TEST 号码一律按测试样张对待
    if inv.is_test_sample:
        inv.notes.append("测试样张：由测试发票生成器生成（满版水印、TEST 号码），非真实发票")
        if inv.invoice_no and not re.fullmatch(r"TEST\w{1,32}", inv.invoice_no):
            inv.issues.append(f"测试样张号码格式不符：{inv.invoice_no}")
    elif inv.invoice_no and not (re.fullmatch(r"\d{20}", inv.invoice_no)
                                 or (re.fullmatch(r"\d{8}", inv.invoice_no) and inv.invoice_code)):
        inv.issues.append(f"发票号码格式不符：{inv.invoice_no}（数电票为 20 位；旧版为 8 位号码加 10/12 位代码）")
    if inv.invoice_code is not None and not inv.is_test_sample and not re.fullmatch(r"\d{10}|\d{12}", inv.invoice_code):
        inv.issues.append(f"发票代码格式不符：{inv.invoice_code}（旧版发票代码为 10 位或 12 位数字）")
    if inv.total_cents is not None:
        if inv.amount_cents is None or inv.tax_cents is None:
            inv.issues.append("未识别到金额或税额，无法核对价税合计")
        elif inv.amount_cents + inv.tax_cents != inv.total_cents:
            inv.issues.append(f"金额 {fmt(inv.amount_cents)} + 税额 {fmt(inv.tax_cents)} ≠ 价税合计 {fmt(inv.total_cents)}")
        if inv.total_upper:
            try:
                if upper_to_cents(inv.total_upper) != inv.total_cents:
                    inv.issues.append(f"大写金额“{inv.total_upper}”与小写 {fmt(inv.total_cents)} 不一致")
            except ValueError as e:
                inv.issues.append(f"大写金额无法解析：{e}")
        else:
            # 正式发票都印有大写金额；缺了就无法完成大小写核对，不导入
            inv.issues.append("未识别到大写金额，无法核对大写与小写一致")
        if inv.total_cents < 0:
            m = re.search(r"被红冲蓝字\S*?(?:发票号码|号码)[:：]?\s*(\d{8,20})", inv.face_remark or "")
            inv.red_of = m.group(1) if m else None
            inv.notes.append("红字发票（价税合计为负数）")
    if not inv.items:
        inv.notes.append("未识别到项目名称")
    if not inv.invoice_type:
        inv.notes.append("未识别到发票类型（专票/普票）")


MAX_BYTES = 10 * 1024 * 1024        # 单个文件上限
MAX_UNZIPPED = 50 * 1024 * 1024     # OFD 解压后总量上限，防压缩炸弹


NO_TEXT = "没有文字层（扫描件或图片），需要识别才能导入：安装 MinerU 并设置 FH_OCR=mineru，见 README“扫描件与图片发票”"


def parse_bytes(name: str, data: bytes, ocr: ocr_mod.OcrFn | None = None) -> ParsedInvoice:
    """ocr 为识别函数（ocr.from_settings），只用于图片和没有文字层的 PDF；不给时这类文件提示需要识别。"""
    inv = ParsedInvoice(file_name=name, sha256=hashlib.sha256(data).hexdigest(), layout="unknown")
    if len(data) > MAX_BYTES:
        inv.issues.append(f"文件超过 {MAX_BYTES // 1024 // 1024} MB")
        return inv
    try:
        if data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
                    inv.issues.append("OFD 解压后体积异常，未解析")
                    return inv
                _parse_ofd(inv, z)
        elif data[:5] == b"%PDF-" or ocr_mod.is_image(data):
            text = ""
            if data[:5] == b"%PDF-":
                from pypdf import PdfReader  # 只有导入 PDF 时才需要
                text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
            if text.strip():
                _parse_text(inv, text)
                if not inv.doc_kind:
                    _check_qr(inv, data)
            elif ocr is None:
                inv.layout = "no_text"
                inv.issues.append(NO_TEXT)
                if _read_qr(inv, data):
                    inv.notes.append("已读取发票二维码；仅凭二维码不能导入，请识别票面并人工核对")
                return inv
            else:
                try:
                    md = ocr(name, data)
                except ocr_mod.OcrError as e:
                    inv.issues.append(f"识别失败：{e}")
                    return inv
                _parse_ocr(inv, md)
                if not inv.doc_kind:  # 小票上的二维码是开票入口，不是发票二维码，不做核对
                    _check_ocr(inv, data, _flatten(md))
        else:
            inv.issues.append("不是 PDF、OFD 或图片文件")
            return inv
    except Exception as e:  # 外部文件格式千奇百怪，任何解析异常都转成问题，交给人复核
        inv.issues.append(f"文件无法解析：{type(e).__name__}: {e}")
        return inv
    _validate(inv)
    return inv


def parse_text(text: str, name: str = "text") -> ParsedInvoice:
    """直接解析已提取的票面文字（测试与排查用）。"""
    inv = ParsedInvoice(file_name=name, sha256=hashlib.sha256(text.encode()).hexdigest(), layout="unknown")
    _parse_text(inv, text)
    _validate(inv)
    return inv


def parse_file(path: Path, ocr: ocr_mod.OcrFn | None = None) -> ParsedInvoice:
    return parse_bytes(path.name, path.read_bytes(), ocr)


def collect(paths: list[str]) -> list[Path]:
    out = []
    for p in map(Path, paths):
        out += sorted(f for f in p.iterdir() if f.suffix.lower() in SUFFIXES) if p.is_dir() else [p]
    return out


# ---------- 入库 ----------

def _content(inv: ParsedInvoice) -> dict:
    return {"issue_date": inv.issue_date, "seller": inv.seller_name, "item": inv.item_summary,
            "amount_cents": inv.total_cents}


# 判断同号票是否“同一张”时比较的票面业务字段（parsed_json 的键）。票面备注、版式、文件名不比：同一张票的
# 电子件和扫描件在这些上本来就会不同
BUSINESS_FIELDS = {"issue_date": "开票日期", "buyer_name": "购买方", "buyer_tax_id": "购买方税号",
                   "seller_name": "销售方", "seller_tax_id": "销售方税号", "items": "项目",
                   "amount": "金额", "tax": "税额", "total": "价税合计"}


def _differences(conn: sqlite3.Connection, company_id: str, inv: ParsedInvoice, existing: sqlite3.Row) -> list[str]:
    """同号票与库内记录不同的字段。库内票来自导入文件时比较全部票面业务字段，来自示例数据时只能比较库表字段。"""
    src = conn.execute("SELECT parsed_json FROM invoice_sources WHERE company_id=? AND invoice_id=?",
                       (company_id, inv.invoice_id)).fetchone()
    if src:
        old, new = json.loads(src["parsed_json"]), inv.view()
        return [f"{label} {old.get(k)} → {new.get(k)}" for k, label in BUSINESS_FIELDS.items() if old.get(k) != new.get(k)]
    old, new = dict(existing), _content(inv)
    return [f"{k} {old[k]} → {new[k]}" for k in new if old[k] != new[k]]


def import_files(conn: sqlite3.Connection, company_id: str, parsed: list[ParsedInvoice], *,
                 commit: bool = False, accept_buyer_mismatch: bool = False,
                 allow_test_samples: bool = False, confirm_ocr: bool = False) -> list[dict]:
    """逐张判定：imported / duplicate / conflict / rejected。commit=False 时只预览，不写库。
    测试样张默认拒收；allow_test_samples 只给测试环境用（FH_ALLOW_TEST_SAMPLES=1 且显式放行）。
    识别件（扫描件、图片）导入时须 confirm_ocr：操作员已对照票面核对代码（如有）、号码和日期，否则标为 needs_confirmation、不入库。"""
    company = conn.execute("SELECT name, short_name FROM companies WHERE company_id=?", (company_id,)).fetchone()
    if not company:
        raise ValueError(f"企业 {company_id} 不存在")
    results = []
    conn.execute("BEGIN")
    try:
        for inv in parsed:
            r = {"file": inv.file_name, "invoice_id": inv.invoice_id, "issues": list(inv.issues), "notes": inv.notes}
            if inv.is_test_sample:
                (r["notes"].append("测试模式：已放行测试样张") if allow_test_samples else
                 r["issues"].append("测试样张不是真实发票，默认不导入（测试时设置 FH_ALLOW_TEST_SAMPLES=1 并显式放行）"))
            if not inv.issues and inv.buyer_name not in (company["name"], company["short_name"]):
                msg = f"购买方“{inv.buyer_name}”不是 {company['name']}"
                (r["notes"] if accept_buyer_mismatch else r["issues"]).append(msg)
            # 本批前面已暂存的票在同一事务里也查得到，所以批内同号同样按内容判重复或冲突；被拒的票不入库，不占号
            existing = conn.execute("SELECT issue_date, seller, item, amount_cents FROM invoices "
                                    "WHERE company_id=? AND invoice_id=?", (company_id, inv.invoice_id)).fetchone()
            if not r["issues"] and inv.total_cents is not None and inv.total_cents < 0:
                blue = inv.red_of and conn.execute(
                    "SELECT 1 FROM invoices WHERE company_id=? AND (invoice_id=? OR invoice_id LIKE ?)",
                    (company_id, inv.red_of, f"%-{inv.red_of}")).fetchone()
                r["notes"].append(f"冲销蓝字发票 {inv.red_of}" if blue else
                                  f"库内找不到对应的蓝字发票{'（票面写明 ' + inv.red_of + '）' if inv.red_of else ''}，请复核")
            if r["issues"]:
                r["status"] = "rejected"
            elif existing and not (diff := _differences(conn, company_id, inv, existing)):
                r["status"] = "duplicate"
            elif existing:
                r["status"] = "conflict"
                r["issues"].append(f"库内或本批前面的文件中已有同号发票，内容不同：{'；'.join(diff)}")
            elif inv.layout == OCR_LAYOUT and commit and not confirm_ocr:
                r["status"] = "needs_confirmation"
                r["notes"].append("识别件未导入：请对照票面核对发票代码（如有）、号码和日期，确认后再导入（命令行加 --confirm-ocr，网页勾选确认）")
            else:
                r["status"] = "imported" if commit else "would_import"
                if inv.layout == OCR_LAYOUT and not commit:
                    r["notes"].append("识别件：导入时须确认已对照票面核对发票代码（如有）、号码和日期")
                now = datetime.now().isoformat(timespec="seconds")
                c = _content(inv)
                conn.execute("INSERT INTO invoices (company_id, invoice_id, issue_date, seller, item, amount_cents, "
                             "updated_at) VALUES (?,?,?,?,?,?,?)",
                             (company_id, inv.invoice_id, c["issue_date"], c["seller"], c["item"], c["amount_cents"], now))
                conn.execute("INSERT INTO invoice_sources (company_id, invoice_id, sha256, file_name, layout, "
                             "parsed_json, imported_at) VALUES (?,?,?,?,?,?,?)",
                             (company_id, inv.invoice_id, inv.sha256, inv.file_name, inv.layout,
                              json.dumps(inv.view(), ensure_ascii=False), now))
            results.append(r)
        conn.execute("COMMIT" if commit else "ROLLBACK")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return results


def require_write(conn: sqlite3.Connection, user_id: str, company_id: str) -> None:
    """导入会新增发票，按“可查看并修改”权限把关。"""
    row = conn.execute("SELECT level FROM permissions WHERE user_id=? AND company_id=?", (user_id, company_id)).fetchone()
    if not row or row["level"] != "write":
        raise PermissionError(f"{user_id} 对 {company_id} 没有修改权限，不能导入发票")


def record_import_event(conn: sqlite3.Connection, session_id: str, company_id: str, results: list[dict],
                        parsed: list[ParsedInvoice]) -> str | None:
    """把导入结果作为系统事件写进会话，模型下一轮能看到“刚导入了哪些票”。事件不算用户原话，不构成修改授权。"""
    done = [p for r, p in zip(results, parsed) if r["status"] == "imported"]
    if not done:
        return None
    total = sum(p.total_cents for p in done)
    tests = sum(p.is_test_sample for p in done)
    text = (f"操作员通过导入功能新增了 {len(done)} 张发票（价税合计 {fmt(total)} 元，用途与备注为空）："
            + "；".join(f"{p.invoice_id} {p.issue_date} {fmt(p.total_cents)} 元" for p in done)
            + (f"。其中 {tests} 张是测试样张（号码以 TEST 开头），不是真实发票" if tests else "")
            + "。具体数据以查询工具为准。")
    conn.execute("INSERT INTO messages (session_id, company_id, role, content, created_at) VALUES (?,?,?,?,?)",
                 (session_id, company_id, "event", text, datetime.now().isoformat(timespec="seconds")))
    return text
