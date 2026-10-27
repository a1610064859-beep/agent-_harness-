"""随机生成虚构的买卖方与明细。公司名、税号、账号均为虚构，税号以 TEST 开头。"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from .models import TEST_PREFIX, Invoice, LineItem, Party

CITIES = ["北辰", "南湾", "东岚", "西岭", "青川", "白鹭", "云泽", "临海"]
WORDS = ["星禾", "启明", "远帆", "知微", "拾光", "青岩", "合众", "砺行", "澄明", "易达"]
TRADES = ["科技", "贸易", "咨询", "文化传媒", "餐饮管理", "建筑工程", "物流", "信息技术"]
SURNAMES = ["王", "李", "张", "刘", "陈", "杨", "赵", "周"]

# (名称, 规格, 单位, 单价区间, 税率)
CATALOG = [
    ("*信息技术服务*软件开发服务", "", "项", (2000, 50000), "0.06"),
    ("*现代服务*咨询服务费", "", "次", (500, 20000), "0.06"),
    ("*计算机配套产品*无线鼠标", "M220", "个", (39, 199), "0.13"),
    ("*计算机配套产品*机械键盘", "K87", "个", (199, 899), "0.13"),
    ("*纸制品*A4复印纸", "70g 500张", "包", (18, 35), "0.13"),
    ("*餐饮服务*餐费", "", "次", (80, 2000), "0.06"),
    ("*住宿服务*住宿费", "标准间", "晚", (180, 800), "0.06"),
    ("*运输服务*国内道路货物运输", "", "车次", (300, 5000), "0.09"),
    ("*农产品*大米", "5kg", "袋", (30, 80), "0.09"),
    ("*印刷品*宣传册印刷", "A5 32P", "本", (3, 15), "0.13"),
]


def fake_tax_id(rng: random.Random) -> str:
    # 18 位，与真实统一社会信用代码等长但以 TEST 开头，必然无法通过校验
    return TEST_PREFIX + "".join(rng.choice("0123456789") for _ in range(14))


def fake_party(rng: random.Random) -> Party:
    city = rng.choice(CITIES)
    name = f"{city}{rng.choice(WORDS)}{rng.choice(TRADES)}有限公司（测试）"
    phone = f"0{rng.randint(10, 99)}-{rng.randint(10000000, 99999999)}"
    addr = f"{city}市测试路{rng.randint(1, 999)}号 {phone}"
    bank = f"{city}测试银行{rng.choice(WORDS)}支行 6200{rng.randint(10**11, 10**12 - 1)}"
    return Party(name=name, tax_id=fake_tax_id(rng), address_phone=addr, bank_account=bank)


def fake_items(rng: random.Random, n: int) -> list[LineItem]:
    items = []
    for name, spec, unit, (lo, hi), rate in rng.sample(CATALOG, n):
        qty = 1 if unit in ("项", "次") else rng.randint(1, 50)
        price = Decimal(rng.randint(lo * 100, hi * 100)) / 100
        items.append(LineItem(name=name, spec=spec, unit=unit, quantity=qty,
                              unit_price=price, tax_rate=rate))
    return items


def fake_invoice(rng: random.Random, seq: int, start: date | None = None,
                 kind: str | None = None, buyer: Party | None = None, days: int = 90,
                 defect: str = "") -> Invoice:
    """开票日期在 start 往前 days 天内；buyer 不给时随机虚构。"""
    start = start or date.today()
    issue = start - timedelta(days=rng.randint(0, days))
    return Invoice(
        number=f"{TEST_PREFIX}{issue:%Y%m%d}{seq:06d}",
        issue_date=issue,
        buyer=buyer or fake_party(rng),
        seller=fake_party(rng),
        items=fake_items(rng, rng.randint(1, 5)),
        kind=kind or rng.choice(["增值税普通发票", "增值税专用发票"]),
        remark="测试数据，仅供软件测试使用",
        issuer=rng.choice(SURNAMES) + "测试",
        defect=defect,
    )
