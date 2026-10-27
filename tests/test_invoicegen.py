import json
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoicegen.amount_words import to_rmb_upper
from invoicegen.cli import main
from invoicegen.fake_data import fake_invoice
from invoicegen.models import Invoice, LineItem, Party

SAMPLE = Path(__file__).resolve().parents[1] / "invoicegen" / "examples" / "sample.json"


@pytest.mark.parametrize("value, expected", [
    ("0", "零元整"),
    ("0.05", "伍分"),
    ("0.50", "伍角整"),
    ("1", "壹元整"),
    ("1.05", "壹元零伍分"),
    ("10", "壹拾元整"),
    ("101", "壹佰零壹元整"),
    ("1000", "壹仟元整"),
    ("1010.10", "壹仟零壹拾元壹角整"),
    ("10001", "壹万零壹元整"),
    ("100010000", "壹亿零壹万元整"),
    ("100001000", "壹亿零壹仟元整"),
    ("12345678.90", "壹仟贰佰叁拾肆万伍仟陆佰柒拾捌元玖角整"),
    ("11290.70", "壹万壹仟贰佰玖拾元柒角整"),
])
def test_rmb_upper(value, expected):
    assert to_rmb_upper(value) == expected


def _party():
    return Party(name="甲（测试）", tax_id="TEST00000000000001")


def test_amount_and_tax_rounding():
    item = LineItem(name="x", quantity=3, unit_price="0.335", tax_rate="0.13")
    assert item.amount == Decimal("1.01")   # 1.005 -> 1.01 四舍五入
    assert item.tax == Decimal("0.13")


def test_number_forced_test_prefix():
    inv = Invoice(number="12345", issue_date=date(2026, 1, 1), buyer=_party(),
                  seller=_party(), items=[LineItem("x", 1, 1, "0.06")])
    assert inv.number == "TEST12345"


def test_rejects_bad_rate_and_empty_items():
    with pytest.raises(ValueError):
        LineItem("x", 1, 1, "0.17")
    with pytest.raises(ValueError):
        Invoice(number="TEST1", issue_date=date(2026, 1, 1), buyer=_party(), seller=_party())


def test_fake_invoice_is_reproducible_and_marked():
    a = fake_invoice(random.Random(7), 1, date(2026, 10, 27)).to_dict()
    b = fake_invoice(random.Random(7), 1, date(2026, 10, 27)).to_dict()
    assert a == b
    assert a["is_test_sample"] is True
    assert a["number"].startswith("TEST")
    assert a["buyer"]["tax_id"].startswith("TEST") and len(a["buyer"]["tax_id"]) == 18
    assert Decimal(a["grand_total"]) == Decimal(a["total_amount"]) + Decimal(a["total_tax"])


def test_json_roundtrip():
    inv = fake_invoice(random.Random(1), 3)
    again = Invoice.from_dict(json.loads(json.dumps(inv.to_dict())))
    assert again.to_dict() == inv.to_dict()


def test_cli_random_and_render(tmp_path):
    assert main(["random", "-n", "3", "--seed", "1", "-o", str(tmp_path / "r")]) == 0
    assert len(list((tmp_path / "r").glob("*.pdf"))) == 3
    assert (tmp_path / "r" / "index.csv").exists()

    assert main(["render", str(SAMPLE), "-o", str(tmp_path / "j")]) == 0
    data = json.loads((tmp_path / "j" / "TEST20261027000001.json").read_text(encoding="utf-8"))
    assert data["grand_total"] == "11615.31"   # 10600 + 898.50 + 116.81(116.805 四舍五入)
    assert (tmp_path / "j" / "TEST20261027000001.pdf").read_bytes().startswith(b"%PDF")


def test_defect_roundtrips_and_is_validated():
    inv = fake_invoice(random.Random(1), 1, defect="upper")
    assert Invoice.from_dict(inv.to_dict()).defect == "upper"
    with pytest.raises(ValueError):
        fake_invoice(random.Random(1), 1, defect="stamp")


def test_cli_buyer_and_date_window(tmp_path):
    assert main(["random", "-n", "4", "--seed", "2", "--buyer-name", "甲企业（比赛模拟）", "--date", "2026-09-30",
                 "--days", "29", "--format", "json", "-o", str(tmp_path)]) == 0
    rows = [json.loads(f.read_text(encoding="utf-8")) for f in tmp_path.glob("*.json")]
    assert {r["buyer"]["name"] for r in rows} == {"甲企业（比赛模拟）"}
    assert all(r["issue_date"].startswith("2026-09") and r["buyer"]["tax_id"].startswith("TEST") for r in rows)
