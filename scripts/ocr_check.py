"""识别自检：把有文字层的电子发票 PDF 转成图片（清晰一张、模拟差扫描一张），走识别，再与该 PDF 自己的文字层
解析结果逐字段比对。不需要人工标注。

    python scripts/ocr_check.py 发票目录/ [更多文件或目录] [--no-degrade]

最要紧的一项是“通过校验但有字段错误”：应当为 0。invoicegen 测试样张满版水印，会挡住文字，不适合用来测识别。
需要先运行 scripts/setup-ocr.bat（或在 .env 中设置 FH_OCR=mineru）。
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fiscal_harness import invoice_files as inf, ocr  # noqa: E402
from fiscal_harness.config import load_settings  # noqa: E402

FIELDS = ("invoice_id", "issue_date", "buyer_name", "buyer_tax_id", "seller_name", "seller_tax_id",
          "amount_cents", "tax_cents", "total_cents")


def images(pdf: bytes, degrade: bool):
    import pypdfium2 as pdfium
    from PIL import ImageFilter

    img = pdfium.PdfDocument(pdf)[0].render(scale=200 / 72).to_pil().convert("RGB")
    out = [("清晰", img, "PNG")]
    if degrade:  # 灰度、倾斜 1.5°、轻微模糊、低质量 JPEG
        out.append(("差扫描", img.convert("L").rotate(1.5, expand=True, fillcolor=255)
                    .filter(ImageFilter.GaussianBlur(0.8)), "JPEG"))
    for label, im, fmt in out:
        buf = io.BytesIO()
        im.save(buf, fmt, **({"quality": 55} if fmt == "JPEG" else {}))
        yield label, buf.getvalue(), ".jpg" if fmt == "JPEG" else ".png"


def main(argv: list[str]) -> int:
    degrade = "--no-degrade" not in argv
    paths = [a for a in argv if not a.startswith("--")]
    recognizer = ocr.from_settings(load_settings())
    if not recognizer:
        print("未开启识别：先运行 scripts\\setup-ocr.bat，或在 .env 中设置 FH_OCR=mineru")
        return 2
    files = [f for f in inf.collect(paths) if f.suffix.lower() == ".pdf"]
    if not files:
        print("用法：python scripts/ocr_check.py <电子发票 PDF 文件或目录>")
        return 2
    right = total = passed = false_accept = test_samples = 0
    for f in files:
        truth = inf.parse_file(f)
        if truth.is_test_sample:
            test_samples += 1
            print(f"跳过 {f.name}：测试样张（水印会挡字，不能当标准答案）")
            continue
        if truth.layout == "no_text" or truth.issues:
            print(f"跳过 {f.name}：文字层本身没有可靠的标准答案（{truth.layout}，{truth.issues[:1]}）")
            continue
        for label, data, suffix in images(f.read_bytes(), degrade):
            start = time.monotonic()
            p = inf.parse_bytes(f.stem + suffix, data, recognizer)
            secs = time.monotonic() - start
            wrong = [k for k in FIELDS if getattr(p, k) != getattr(truth, k)]
            right, total = right + len(FIELDS) - len(wrong), total + len(FIELDS)
            ok = not p.issues
            passed += ok
            false_accept += ok and bool(wrong)
            verdict = "通过校验" if ok else "被拦下"
            print(f"{f.name} [{label}] {len(FIELDS) - len(wrong)}/{len(FIELDS)} 字段正确 · {verdict} · {secs:.1f} 秒")
            for k in wrong:
                print(f"    ✕ {k}: 识别 {getattr(p, k)!r}，应为 {getattr(truth, k)!r}")
            for x in p.issues:
                print(f"    · {x}")
    if not total:
        print("没有可比对的文件。" + (f"其中 {test_samples} 份是 invoicegen 测试样张，只能用来验证测试票被拒收，"
                                     "不能衡量识别准确率。请改用真实的电子发票 PDF（有文字层，如电子税务局下载的数电票）。"
                                     if test_samples else ""))
        return 2
    n = total // len(FIELDS)
    print(f"\n字段正确 {right}/{total}；通过校验 {passed}/{n} 张；通过校验但有字段错误：{false_accept} 张（应为 0）")
    return 1 if false_accept else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
