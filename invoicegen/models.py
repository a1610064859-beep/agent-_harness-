"""发票数据模型与金额计算。所有金额用 Decimal，按分四舍五入。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ALLOWED_TAX_RATES = {Decimal(r) for r in ("0", "0.01", "0.03", "0.06", "0.09", "0.13")}
TEST_PREFIX = "TEST"
# 故意做错的票面，用来测下游的校验：只改 PDF 上印的数字，JSON 里的金额仍是正确值
DEFECTS = {
    "total": "价税合计（小写）比金额 + 税额多 0.01，大写随小写",
    "upper": "大写金额比小写多 1 元",
}


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Party:
    name: str
    tax_id: str
    address_phone: str = ""
    bank_account: str = ""


@dataclass
class LineItem:
    name: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate: Decimal
    spec: str = ""
    unit: str = ""

    def __post_init__(self):
        self.quantity = Decimal(str(self.quantity))
        self.unit_price = Decimal(str(self.unit_price))
        self.tax_rate = Decimal(str(self.tax_rate))
        if self.tax_rate not in ALLOWED_TAX_RATES:
            raise ValueError(f"不支持的税率: {self.tax_rate}")
        if self.quantity <= 0 or self.unit_price < 0:
            raise ValueError(f"数量须为正、单价不能为负: {self.name}")

    @property
    def amount(self) -> Decimal:
        return money(self.quantity * self.unit_price)

    @property
    def tax(self) -> Decimal:
        return money(self.amount * self.tax_rate)


@dataclass
class Invoice:
    number: str
    issue_date: date
    buyer: Party
    seller: Party
    items: list[LineItem] = field(default_factory=list)
    kind: str = "增值税普通发票"
    remark: str = ""
    issuer: str = "测试员"
    defect: str = ""

    def __post_init__(self):
        # 防止生成的文件被当成真实发票：号码必须带 TEST 前缀
        if not self.number.startswith(TEST_PREFIX):
            self.number = TEST_PREFIX + self.number
        if not self.items:
            raise ValueError("发票至少需要一行明细")
        if self.defect and self.defect not in DEFECTS:
            raise ValueError(f"不支持的缺陷类型: {self.defect}")

    @property
    def total_amount(self) -> Decimal:
        return sum((i.amount for i in self.items), Decimal("0.00"))

    @property
    def total_tax(self) -> Decimal:
        return sum((i.tax for i in self.items), Decimal("0.00"))

    @property
    def grand_total(self) -> Decimal:
        return self.total_amount + self.total_tax

    def to_dict(self) -> dict:
        d = asdict(self)
        d["issue_date"] = self.issue_date.isoformat()
        d["is_test_sample"] = True
        for raw, item in zip(d["items"], self.items):
            raw.update(
                quantity=str(item.quantity),
                unit_price=str(item.unit_price),
                tax_rate=str(item.tax_rate),
                amount=str(item.amount),
                tax=str(item.tax),
            )
        d["total_amount"] = str(self.total_amount)
        d["total_tax"] = str(self.total_tax)
        d["grand_total"] = str(self.grand_total)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Invoice":
        item_keys = ("name", "quantity", "unit_price", "tax_rate", "spec", "unit")
        return cls(
            number=d["number"],
            issue_date=date.fromisoformat(d["issue_date"]),
            buyer=Party(**d["buyer"]),
            seller=Party(**d["seller"]),
            items=[LineItem(**{k: i[k] for k in item_keys if k in i}) for i in d["items"]],
            kind=d.get("kind", "增值税普通发票"),
            remark=d.get("remark", ""),
            issuer=d.get("issuer", "测试员"),
            defect=d.get("defect", ""),
        )
