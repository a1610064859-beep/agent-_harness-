"""运行配置：只从环境变量和项目根目录的 .env 读取，密钥不写入任何日志。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    sample_xlsx: Path
    rules_docx: Path
    llm_provider: str
    llm_base_url: str
    llm_model: str
    llm_effort: str | None
    max_steps: int
    history_chars: int
    variant: str
    today: date
    allow_foreign_buyer: bool = False  # 导入时允许购买方不是本企业，仅测试外部样本用
    allow_test_samples: bool = False   # 导入时允许 invoicegen 生成的测试样张，仅测试用
    file_import: bool = False          # 附加功能：导入发票文件与对话中上传（初赛范围外），FH_FILE_IMPORT=1 开启
    ocr: str = ""                      # 扫描件/图片识别：mineru 开启，默认关闭
    mineru_bin: str = "mineru"         # MinerU 命令行路径（scripts/setup-ocr.bat 会写进 .env）
    ocr_tier: str | None = None        # MinerU 档位：basic / standard / advanced，不填由 MinerU 决定
    ocr_timeout: int = 300             # 单张识别的最长等待秒数

    @property
    def api_key(self) -> str:
        # 每次现取，避免密钥被 dataclass repr 或运行记录带出
        return os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")


def load_settings() -> Settings:
    today = os.environ.get("FH_TODAY")
    return Settings(
        db_path=Path(os.environ.get("FH_DB", ROOT / "data" / "harness.db")),
        sample_xlsx=Path(os.environ.get("FH_SAMPLE_XLSX", ROOT / "data" / "source" / "sample_data_v1.1.0.xlsx")),
        rules_docx=Path(os.environ.get("FH_RULES_DOCX", ROOT / "data" / "source" / "business_rules_v1.1.0.docx")),
        llm_provider=os.environ.get("LLM_PROVIDER", "openai_compat"),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        llm_model=os.environ.get("LLM_MODEL", "deepseek-flash"),
        llm_effort=os.environ.get("LLM_EFFORT") or None,
        max_steps=int(os.environ.get("FH_MAX_STEPS", "8")),
        history_chars=int(os.environ.get("FH_HISTORY_CHARS", "6000")),
        # 对比实验：baseline=规则由程序解析；model_rules=把全部规则版本原样交给模型自行判断
        variant=os.environ.get("FH_VARIANT", "baseline"),
        today=date.fromisoformat(today) if today else date.today(),
        allow_foreign_buyer=os.environ.get("FH_ALLOW_FOREIGN_BUYER") == "1",
        allow_test_samples=os.environ.get("FH_ALLOW_TEST_SAMPLES") == "1",
        file_import=os.environ.get("FH_FILE_IMPORT") == "1",
        ocr=os.environ.get("FH_OCR", "").strip().lower(),
        mineru_bin=os.environ.get("FH_MINERU_BIN", "mineru"),
        ocr_tier=os.environ.get("FH_OCR_TIER") or None,
        ocr_timeout=int(os.environ.get("FH_OCR_TIMEOUT", "300")),
    )
