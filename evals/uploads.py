"""评测用的对话附件：随某轮用户消息上传的发票，走正式的解析与校验，导入由模型按用户原话调用工具。

对话上传不放行测试样张，所以用单元测试里的票面固定件：旧版电子普票文字层（kv），
和由识别替身给出票面文字的数电票图片（scan，识别件），购买方都是甲企业。
"""
from __future__ import annotations

from fiscal_harness import invoice_files
from tests.test_invoice_files import KV
from tests.test_ocr import OCR_DIGITAL, fake, png

KV_ID = "012345678901-12345678"
SCAN_ID = "26312000000000000001"

ATTACHMENTS = {
    "kv": lambda: invoice_files.parse_text(KV, "kv.pdf"),
    "scan": lambda: invoice_files.parse_bytes("scan.png", png(), ocr=fake(OCR_DIGITAL)),
}


def build(keys) -> list[invoice_files.ParsedInvoice]:
    return [ATTACHMENTS[k]() for k in keys]
