"""金额统一用整数分存储、Decimal 计算，对外输出两位小数字符串（R001 第 3 条）。"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_cents(value) -> int:
    if isinstance(value, float):
        value = repr(value)  # 避免 float 二进制误差进入 Decimal
    return int((Decimal(str(value)).quantize(CENT, ROUND_HALF_UP) * 100).to_integral_value())


def fmt(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"
