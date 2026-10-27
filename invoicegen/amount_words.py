"""人民币金额转中文大写，例如 1234.50 -> 壹仟贰佰叁拾肆元伍角整。"""
from .models import money

DIGITS = "零壹贰叁肆伍陆柒捌玖"
UNITS = ["", "拾", "佰", "仟"]
SECTIONS = ["", "万", "亿", "万亿"]


def _section(n: int) -> str:
    """0 < n < 10000 的一节。"""
    out, zero = "", False
    for pos in range(3, -1, -1):
        d = n // 10**pos % 10
        if d == 0:
            zero = bool(out)
        else:
            if zero:
                out += "零"
                zero = False
            out += DIGITS[d] + UNITS[pos]
    return out


def to_rmb_upper(value) -> str:
    amt = money(value)
    if amt < 0:
        return "负" + to_rmb_upper(-amt)
    yuan = int(amt)
    fen_total = int((amt - yuan) * 100)
    jiao, fen = divmod(fen_total, 10)

    if yuan == 0:
        int_part = ""
    else:
        parts, need_zero, idx, n = [], False, 0, yuan
        while n:
            sec = n % 10000
            if sec:
                s = _section(sec) + SECTIONS[idx]
                if need_zero:
                    s += "零"
                parts.append(s)
                need_zero = sec < 1000
            else:
                need_zero = bool(parts)
            n //= 10000
            idx += 1
        int_part = "".join(reversed(parts)) + "元"

    if fen_total == 0:
        return (int_part or "零元") + "整"
    dec = ""
    if jiao:
        dec += DIGITS[jiao] + "角"
    elif int_part:
        dec += "零"
    if fen:
        dec += DIGITS[fen] + "分"
    else:
        dec += "整"
    return int_part + dec

