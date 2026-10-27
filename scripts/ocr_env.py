"""把识别设置写进 .env（已有的同名项就地替换，其他内容不动）。setup-ocr.bat 调用。

用法：python scripts/ocr_env.py <mineru 路径> <档位> <MINERU_HOME>
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    mineru, tier, home = argv
    # MINERU_HOME 也写进 .env：harness 调用 MinerU 时从 .env 读到它，模型和缓存就留在同一处
    values = {"FH_OCR": "mineru", "FH_MINERU_BIN": mineru, "FH_OCR_TIER": tier, "MINERU_HOME": home}
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    for key, value in values.items():
        line = f"{key}={value}"
        hits = [i for i, ln in enumerate(lines) if re.match(rf"\s*#?\s*{key}\s*=", ln)]
        if hits:
            lines[hits[0]] = line
        else:
            lines.append(line)
        print(f"[setup-ocr] .env: {line}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
