"""生成参赛提交压缩包：python scripts/package.py

只打包 git 已跟踪的文件（因此不含 .env、虚拟环境、数据库），并排除内部规划与审查材料。
打包前扫描全部文件，发现疑似模型密钥立即中止。
"""
from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE = (
    "materials/",                         # 原始材料与早期规划（data/source 已含运行所需的官方资料）
    "docs/development-review-plan",       # 内部开发规划
    "docs/reviews/",                      # 内部审查往来
    "scripts/package.py",
)
KEY = re.compile(rb"sk-[A-Za-z0-9]{20,}")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from fiscal_harness import __version__

    files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    files = [f for f in files if f and not f.startswith(EXCLUDE)]
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *files], cwd=ROOT, capture_output=True, text=True).stdout
    if dirty.strip():
        print("以下已跟踪文件有未提交的修改，请先提交再打包：\n" + dirty)
        return 1
    for f in files:
        if KEY.search((ROOT / f).read_bytes()):
            print(f"发现疑似密钥：{f}，已中止。")
            return 1
    out = ROOT / "dist" / f"fiscal-agent-harness-{__version__}-{datetime.now():%Y%m%d}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(ROOT / f, f"fiscal-agent-harness/{f}")
    print(f"已生成 {out}（{len(files)} 个文件，{out.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
