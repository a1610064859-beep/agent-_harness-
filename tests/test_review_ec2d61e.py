"""ec2d61e 复评（E:\\claude\\inbox\\fiscal-agent-harness-ec2d61e-review）提出的 3 项问题：评审探针转成回归测试，
另补正常说法不被误拦的用例。模型调用一律用预设参数，不调用真实接口。"""
import pytest

from fiscal_harness import invoice_files as inf
from fiscal_harness.tools import ToolError

from .test_invoice_files import DIGITAL, KV
from .test_review_regressions import fresh

LEGACY_A, LEGACY_B = "012345678901-12345678", "012345678902-12345678"


def field(conn, invoice, name, company="C001"):
    return conn.execute(f"SELECT {name} FROM invoices WHERE company_id=? AND invoice_id=?",
                        (company, invoice)).fetchone()[0]


def rejects(box, code, invoice, quote, **changes):
    with pytest.raises(ToolError) as e:
        box.update_invoice(invoice, user_quote=quote, **changes)
    assert e.value.code == code, e.value


# ---------- 1. [P1] 完整编号已点名，不能改另一张同号码的旧版发票 ----------

def _two_legacy(conn):
    r = inf.import_files(conn, "C001", [inf.parse_text(KV), inf.parse_text(KV.replace("012345678901", "012345678902"))],
                         commit=True)
    assert [x["status"] for x in r] == ["imported", "imported"]


def test_full_legacy_id_does_not_match_another_code(conn):
    _two_legacy(conn)
    text = f"请把{LEGACY_A}用途写软件服务并保存"
    _, box = fresh(conn, text)
    assert box._named_invoices(text) == {LEGACY_A}
    rejects(box, "INVOICE_NOT_REQUESTED", LEGACY_B, text, purpose="软件服务")
    assert field(conn, LEGACY_B, "purpose") is None
    assert box.update_invoice(LEGACY_A, user_quote=text, purpose="软件服务")["verified"]


def test_ambiguous_short_legacy_number_asks_for_code(conn):
    _two_legacy(conn)
    text = "号码12345678那张用途写软件服务，保存"
    _, box = fresh(conn, text)
    assert box._named_invoices(text) == set()
    rejects(box, "INVOICE_AMBIGUOUS", LEGACY_A, text, purpose="软件服务")


def test_unique_short_legacy_number_still_works(conn):
    inf.import_files(conn, "C001", [inf.parse_text(KV)], commit=True)
    text = "号码12345678那张用途写软件服务，保存"
    _, box = fresh(conn, text)
    assert box.update_invoice(LEGACY_A, user_quote=text, purpose="软件服务")["verified"]


def test_invoice_id_must_stand_alone(conn):
    _, box = fresh(conn, "F0021 这张不存在，F002 用途填打印复印纸")
    assert box._named_invoices("F0021 这张不存在") == set()


# ---------- 2. [P1] 一句话多张票、多个字段时，值不能互换 ----------

def test_values_cannot_be_swapped_between_invoices(conn):
    text = "9月F002用途填打印复印纸；F003用途填寄送合同快递，两张都保存"
    _, box = fresh(conn, text)
    rejects(box, "VALUE_NOT_FOR_TARGET", "F002", text, purpose="寄送合同快递")
    rejects(box, "VALUE_NOT_FOR_TARGET", "F003", text, purpose="打印复印纸")
    assert field(conn, "F002", "purpose") is None and field(conn, "F003", "purpose") is None
    assert box.update_invoice("F002", user_quote=text, purpose="打印复印纸")["verified"]
    assert box.update_invoice("F003", user_quote=text, purpose="寄送合同快递")["verified"]


def test_values_cannot_be_swapped_between_fields(conn):
    text = "9月F002用途填打印复印纸，备注填市场部，保存"
    _, box = fresh(conn, text)
    rejects(box, "VALUE_NOT_FOR_TARGET", "F002", text, purpose="市场部", remark="打印复印纸")
    assert field(conn, "F002", "purpose") is None and field(conn, "F002", "remark") is None
    assert box.update_invoice("F002", user_quote=text, purpose="打印复印纸", remark="市场部")["verified"]


@pytest.mark.parametrize("text,writes", [
    ("9月F002和F003的用途都填办公用品，保存", [("F002", "purpose"), ("F003", "purpose")]),
    ("9月F002、F003用途都填办公用品，保存", [("F002", "purpose"), ("F003", "purpose")]),
    ("9月F002的用途和备注都填办公用品，保存", [("F002", "purpose"), ("F002", "remark")]),
])
def test_shared_values_are_allowed(conn, text, writes):
    _, box = fresh(conn, text)
    for invoice, name in writes:
        assert box.update_invoice(invoice, user_quote=text, **{name: "办公用品"})["verified"]


def test_unlabeled_value_means_purpose(conn):
    text = "9月F002用途写打印复印纸，F004是出差住宿，保存"
    _, box = fresh(conn, text)
    rejects(box, "VALUE_NOT_FOR_TARGET", "F004", text, remark="出差住宿")
    rejects(box, "VALUE_NOT_FOR_TARGET", "F004", text, purpose="打印复印纸")
    assert box.update_invoice("F004", user_quote=text, purpose="出差住宿")["verified"]


def test_follow_up_without_invoice_name_binds_to_earlier_named_invoice(conn):
    from .conftest import say
    sid, box = fresh(conn, "9月F002用途填打印复印纸；F003用途填寄送合同快递，先别保存")
    say(conn, sid, "C001", "可以了，保存吧")
    rejects(box, "VALUE_NOT_FOR_TARGET", "F002", "可以了，保存吧", purpose="寄送合同快递")
    assert box.update_invoice("F002", user_quote="可以了，保存吧", purpose="打印复印纸")["verified"]


# ---------- 3. [P2] 批量导入按内容判重复或冲突，被拒的文件不占号 ----------

def test_same_batch_same_number_different_content_is_conflict(conn):
    a = inf.parse_text(DIGITAL, "first.ofd")
    b = inf.parse_text(DIGITAL.replace("测试广告有限公司", "另一家测试公司"), "conflicting.ofd")
    r = inf.import_files(conn, "C001", [a, b], commit=True)
    assert [x["status"] for x in r] == ["imported", "conflict"] and "本批" in r[1]["issues"][0]


def test_same_batch_identical_file_is_duplicate(conn):
    r = inf.import_files(conn, "C001", [inf.parse_text(DIGITAL, "a.ofd"), inf.parse_text(DIGITAL, "b.ofd")], commit=True)
    assert [x["status"] for x in r] == ["imported", "duplicate"]


def test_rejected_file_does_not_block_valid_one(conn):
    bad = inf.parse_text(DIGITAL.replace("1691.23", "1691.24"), "damaged.ofd")
    good = inf.parse_text(DIGITAL, "valid.ofd")
    for commit in (False, True):
        r = inf.import_files(conn, "C001", [bad, good], commit=commit)
        assert [x["status"] for x in r] == ["rejected", "imported" if commit else "would_import"]
    assert conn.execute("SELECT COUNT(*) FROM invoices WHERE company_id='C001' AND invoice_id=?",
                        (good.invoice_id,)).fetchone()[0] == 1
