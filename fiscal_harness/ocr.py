"""扫描件与图片发票的识别：调用本机 MinerU 命令行，识别结果交给 invoice_files 解析和校验；另读票面二维码做交叉核对。

默认关闭，设置 FH_OCR=mineru 开启。MinerU 装在单独的虚拟环境里（scripts/setup-ocr.bat），这里只调用它的命令行：
从不加 --remote，发票不上传；识别完用 forget 清掉 MinerU 文档库里的记录；所有命令在 data/ocr/ 下运行，
不往仓库里写文件。识别结果和文字层一样要过程序校验，导入前还须人工确认号码和日期。
"""
from __future__ import annotations

import io
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Callable

from .money import to_cents

IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*", b"BM")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")

OcrFn = Callable[[str, bytes], str]


class OcrError(Exception):
    pass


def is_image(data: bytes) -> bool:
    return data.startswith(IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def mineru(command: list[str], workdir: Path, tier: str | None = None, timeout: int = 300) -> OcrFn:
    """返回 ocr(文件名, 内容) -> Markdown。command 是 MinerU 命令行，如 [".ocr\\venv\\Scripts\\mineru.exe"]。"""
    workdir.mkdir(parents=True, exist_ok=True)

    def run(*args: str, limit: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([*command, *args], cwd=workdir, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=limit)

    def ocr(name: str, data: bytes) -> str:
        suffix = Path(name).suffix.lower() or ".png"
        with tempfile.TemporaryDirectory(dir=workdir) as d:
            src, out = Path(d) / f"invoice{suffix}", Path(d) / "out.md"
            src.write_bytes(data)
            args = ["parse", str(src), "--no-marker", "--wait", str(timeout), "-o", str(out)]
            if tier:
                args += ["--tier", tier]
            deadline, started = time.monotonic() + timeout, False
            try:
                while True:
                    p = run(*args, limit=timeout + 60)
                    if p.returncode == 0 and out.exists():
                        return out.read_text(encoding="utf-8")
                    msg = (p.stderr or p.stdout or "").strip()
                    if "not running" in msg and not started:  # 服务没开：开一次再试
                        run("server", "start")
                        started = True
                    elif "not ready" in msg and time.monotonic() < deadline:  # 模型还在加载
                        time.sleep(3)
                    else:
                        raise OcrError(msg[-300:] or f"MinerU 退出码 {p.returncode}")
            except (OSError, subprocess.TimeoutExpired) as e:
                raise OcrError(f"无法运行 MinerU：{e}") from e
            finally:
                try:
                    run("forget", str(src), "--no-dry-run")
                except (OSError, subprocess.TimeoutExpired):
                    pass

    return ocr


def from_settings(settings) -> OcrFn | None:
    if settings.ocr != "mineru":
        return None
    return mineru([settings.mineru_bin], settings.db_path.parent / "ocr", settings.ocr_tier, settings.ocr_timeout)


# ---------- 二维码 ----------

def read_qr(data: bytes) -> str | None:
    """读票面二维码原文（“01,”开头的发票二维码）。没装 zxing-cpp 时抛 ImportError，读不到返回 None。"""
    import zxingcpp
    from PIL import Image

    if data[:5] == b"%PDF-":
        import pypdfium2 as pdfium
        images = [pdfium.PdfDocument(data)[0].render(scale=200 / 72).to_pil()]
    else:
        images = [Image.open(io.BytesIO(data))]
    for img in images:
        for b in zxingcpp.read_barcodes(img):
            if b.format == zxingcpp.BarcodeFormat.QRCode and b.text.startswith("01,"):
                return b.text
    return None


def qr_fields(text: str) -> dict | None:
    """发票二维码：01,类型,发票代码,发票号码,金额,开票日期YYYYMMDD,校验码,…；数电票的发票代码为空。"""
    parts = text.split(",")
    if len(parts) < 6 or parts[0] != "01":
        return None
    try:
        d = parts[5]
        return {"type": parts[1], "code": parts[2] or None, "number": parts[3],
                "amount_cents": to_cents(parts[4]), "issue_date": date(int(d[:4]), int(d[4:6]), int(d[6:8])).isoformat()}
    except (ValueError, IndexError, ArithmeticError):
        return None
