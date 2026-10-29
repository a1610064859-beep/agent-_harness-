"""发票文件导入与对话上传是附加功能（初赛不要求发票图像识别），默认关闭：模型看不到导入工具，各入口都拒绝。"""
import base64
import dataclasses

import pytest
from fastapi import HTTPException

from fiscal_harness import context, db, harness, importer, web
from fiscal_harness.llm import ScriptedLLM
from fiscal_harness.tools import UPLOAD_TOOLS, Scope, Toolbox, dispatch, tool_specs

from .conftest import TODAY
from .test_chat_uploads import text_invoice
from .test_invoice_files import KV


def test_off_by_default(settings):
    assert settings.file_import is False


@pytest.mark.parametrize("variant", ["baseline", "model_rules"])
def test_model_does_not_see_upload_tools_or_policy(conn, variant):
    assert not UPLOAD_TOOLS & {t["function"]["name"] for t in tool_specs(variant)}
    assert UPLOAD_TOOLS <= {t["function"]["name"] for t in tool_specs(variant, file_import=True)}
    sid = harness.start_session(conn, "U001", "C001")
    scope = Scope(sid, "U001", "C001", TODAY)
    assert "import_uploaded_invoices" not in context.build_system(conn, scope, variant)[0]
    assert "import_uploaded_invoices" in context.build_system(conn, scope, variant, file_import=True)[0]
    res = dispatch(Toolbox(conn, scope), "import_uploaded_invoices", {"upload_ids": ["UP-1"], "user_quote": "导入"})
    assert res["error"]["code"] == "UNKNOWN_TOOL"


def test_turn_with_attachments_is_refused(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    with pytest.raises(ValueError):
        harness.run_turn(conn, ScriptedLLM([]), settings, sid, "导入", attachments=[text_invoice()])
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0] == 0


def test_web_entries_are_closed(settings, tmp_path, monkeypatch):
    s = dataclasses.replace(settings, db_path=tmp_path / "off.db")
    importer.reset_and_import(db.connect(s.db_path), s.sample_xlsx, s.rules_docx)
    monkeypatch.setattr(web, "from_settings", lambda _: ScriptedLLM([]))
    app = web.create_app(s)
    routes = {(r.path, next(iter(r.methods))): r.endpoint for r in app.routes if hasattr(r, "methods")}
    assert routes[("/api/meta", "GET")]()["file_import"] is False
    sid = routes[("/api/sessions", "POST")](web.NewSession(user_id="U001", company_id="C001"))["session_id"]
    f = web.UploadedFile(name="kv.pdf", data=base64.b64encode(KV.encode()).decode())
    for call in (lambda: routes[("/api/sessions/{sid}/messages", "POST")](sid, web.Message(text="导入", files=[f])),
                 lambda: routes[("/api/invoice-files", "POST")](web.InvoiceUpload(session_id=sid, files=[f])),
                 lambda: routes[("/ocr-review", "GET")]()):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 403
