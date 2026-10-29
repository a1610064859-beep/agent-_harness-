"""invoicegen 测试样张接入发票导入：生成 → 解析 → 默认拒收 → 测试模式放行。不调用模型。"""
import base64
import dataclasses
import json
import random
from datetime import date

import pytest
from fastapi import HTTPException

from fiscal_harness import cli, db, importer, invoice_files as inf, web
from fiscal_harness.llm import ScriptedLLM
from fiscal_harness.money import to_cents
from invoicegen.cli import write_batch
from invoicegen.fake_data import fake_invoice
from invoicegen.models import Party

from .test_invoice_files import KV


@pytest.fixture
def settings(settings):
    """发票文件导入是附加功能，默认关闭；这些测试打开它。"""
    return dataclasses.replace(settings, file_import=True)


BUYER = Party(name="甲企业（比赛模拟）", tax_id="TEST0000000000C001")


def _make(tmp_path, n=1, seed=1, defect="", buyer=BUYER):
    rng = random.Random(seed)
    invs = [fake_invoice(rng, i + 1, date(2026, 9, 30), buyer=buyer, days=29, defect=defect) for i in range(n)]
    write_batch(invs, tmp_path)
    return [tmp_path / f"{inv.number}.pdf" for inv in invs]


def test_generated_samples_parse_back_exactly(tmp_path):
    for pdf in _make(tmp_path, n=20, seed=7, buyer=None):
        p, j = inf.parse_file(pdf), json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8"))
        assert p.layout == "test_sample" and p.is_test_sample and p.issues == []
        assert p.invoice_id == j["number"] and p.issue_date == j["issue_date"]
        assert (p.buyer_name, p.buyer_tax_id) == (j["buyer"]["name"], j["buyer"]["tax_id"])
        assert (p.seller_name, p.seller_tax_id) == (j["seller"]["name"], j["seller"]["tax_id"])
        assert (p.amount_cents, p.tax_cents, p.total_cents) == tuple(
            to_cents(j[k]) for k in ("total_amount", "total_tax", "grand_total"))
        assert p.items == [i["name"] for i in j["items"]]
        assert p.invoice_type == f"测试样张·{j['kind']}"


def test_test_samples_are_rejected_unless_allowed(conn, tmp_path):
    parsed = [inf.parse_file(f) for f in _make(tmp_path, n=2)]
    r = inf.import_files(conn, "C001", parsed)
    assert [x["status"] for x in r] == ["rejected", "rejected"]
    assert "FH_ALLOW_TEST_SAMPLES" in r[0]["issues"][0]
    r = inf.import_files(conn, "C001", parsed, allow_test_samples=True, commit=True)
    assert [x["status"] for x in r] == ["imported", "imported"]
    row = conn.execute("SELECT layout, parsed_json FROM invoice_sources WHERE invoice_id=?",
                       (parsed[0].invoice_id,)).fetchone()
    assert row["layout"] == "test_sample" and json.loads(row["parsed_json"])["is_test_sample"] is True
    # 再导一次是重复；同号不同内容是冲突
    again = [inf.parse_file(f) for f in _make(tmp_path, n=2)]
    assert inf.import_files(conn, "C001", again, allow_test_samples=True)[0]["status"] == "duplicate"
    changed = inf.parse_file(_make(tmp_path / "other", n=1, seed=99)[0])
    changed.invoice_no = parsed[0].invoice_no
    assert inf.import_files(conn, "C001", [changed], allow_test_samples=True)[0]["status"] == "conflict"


def test_buyer_check_still_applies_to_test_samples(conn, tmp_path):
    p = inf.parse_file(_make(tmp_path, buyer=None)[0])
    r = inf.import_files(conn, "C001", [p], allow_test_samples=True)[0]
    assert r["status"] == "rejected" and "购买方" in r["issues"][0]


@pytest.mark.parametrize("defect,expect", [("total", "≠ 价税合计"), ("upper", "大写金额")])
def test_injected_defects_are_caught(conn, tmp_path, defect, expect):
    p = inf.parse_file(_make(tmp_path, defect=defect)[0])
    assert len(p.issues) == 1 and expect in p.issues[0]
    assert inf.import_files(conn, "C001", [p], allow_test_samples=True)[0]["status"] == "rejected"


def test_test_number_in_any_layout_is_a_test_sample(conn):
    p = inf.parse_text(KV.replace("发票号码: 12345678", "发票号码: TEST12345678"))
    assert p.layout == "test_sample" and p.is_test_sample and p.invoice_id == "TEST12345678"
    assert inf.import_files(conn, "C001", [p])[0]["status"] == "rejected"


def test_import_event_says_which_are_test_samples(conn, box_for, tmp_path):
    box = box_for(said=())
    parsed = [inf.parse_file(f) for f in _make(tmp_path)]
    results = inf.import_files(conn, "C001", parsed, allow_test_samples=True, commit=True)
    text = inf.record_import_event(conn, box.scope.session_id, "C001", results, parsed)
    assert "1 张是测试样张" in text


def test_web_requires_env_to_allow_test_samples(settings, tmp_path, monkeypatch):
    pdf = _make(tmp_path / "f")[0]
    upload = [web.UploadedFile(name=pdf.name, data=base64.b64encode(pdf.read_bytes()).decode())]
    for allowed in (False, True):
        s = dataclasses.replace(settings, db_path=tmp_path / f"web{allowed}.db", allow_test_samples=allowed)
        importer.reset_and_import(db.connect(s.db_path), s.sample_xlsx, s.rules_docx)
        monkeypatch.setattr(web, "from_settings", lambda _: ScriptedLLM([]))
        app = web.create_app(s)
        routes = {(r.path, next(iter(r.methods))): r.endpoint for r in app.routes if hasattr(r, "methods")}
        sid = routes[("/api/sessions", "POST")](web.NewSession(user_id="U001", company_id="C001"))["session_id"]
        assert routes[("/api/meta", "GET")]()["allow_test_samples"] is allowed
        body = web.InvoiceUpload(session_id=sid, files=upload, allow_test_samples=True)
        if allowed:
            assert routes[("/api/invoice-files", "POST")](body)["results"][0]["status"] == "would_import"
        else:
            with pytest.raises(HTTPException) as e:
                routes[("/api/invoice-files", "POST")](body)
            assert e.value.status_code == 403


def test_cli_generates_for_company_and_gates_import(settings, tmp_path, monkeypatch):
    importer.reset_and_import(db.connect(tmp_path / "h.db"), settings.sample_xlsx, settings.rules_docx)
    monkeypatch.setenv("FH_DB", str(tmp_path / "h.db"))
    monkeypatch.setenv("FH_FILE_IMPORT", "1")
    monkeypatch.delenv("FH_ALLOW_TEST_SAMPLES", raising=False)
    out = tmp_path / "gen"
    cli.main(["data", "gen-test-invoices", "--company", "C002", "--month", "2026-08", "-n", "3", "--seed", "5",
              "--out", str(out)])
    parsed = [inf.parse_file(f) for f in sorted(out.glob("*.pdf"))]
    assert len(parsed) == 3 and {p.buyer_name for p in parsed} == {"乙企业（比赛模拟）"}
    assert all(p.issue_date.startswith("2026-08") and not p.issues for p in parsed)
    with pytest.raises(SystemExit):
        cli.main(["data", "import-files", "--company", "C002", "--files", str(out), "--allow-test-samples"])
    monkeypatch.setenv("FH_ALLOW_TEST_SAMPLES", "1")
    cli.main(["data", "import-files", "--company", "C002", "--files", str(out), "--allow-test-samples", "--commit"])
    n = db.connect(tmp_path / "h.db").execute(
        "SELECT count(*) FROM invoices WHERE company_id='C002' AND invoice_id LIKE 'TEST%'").fetchone()[0]
    assert n == 3


def test_eval_import_fixture_expectations(conn, box_for):
    """evals 导入类案例的标准答案由程序算出，这里先确定性地核对一遍。"""
    from evals.imports import import_samples
    from fiscal_harness.tools import ToolError, purpose_status
    boxes = {c: box_for(company=c, said=()) for c in ("C001", "C002")}
    for company, box in boxes.items():
        assert "2 张是测试样张" in import_samples(conn, box.scope.session_id, company, ("A", "B"))
    with pytest.raises(ToolError) as e:  # 事件里有 9 月日期，但用户没说月份
        boxes["C001"].check_purpose(month="2026-09")
    assert e.value.code == "MONTH_UNCONFIRMED"
    missing = lambda c: [r["invoice_id"] for r in purpose_status(conn, c, "2026-09")["missing_required"]]
    assert missing("C001") == ["F002", "F003", "F004", "F006", "F009", "TEST20260918000001", "TEST20260925000002"]
    assert missing("C002") == ["F002", "F005", "F006", "TEST20260925000002"]
    assert box_for().summarize_month(month="2026-09")["invoice_total"] == "11494.56"
