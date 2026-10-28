"""对话中上传发票（PDF、OFD、图片）：程序解析校验后暂存，用户本轮明确要求才导入；识别件须下一句确认已核对票面。
模型用预设脚本，识别函数用替身，不调用真实接口。"""
import base64
import dataclasses

import pytest
from fastapi import HTTPException

from fiscal_harness import db, harness, importer, web
from fiscal_harness import invoice_files as inf
from fiscal_harness.llm import ScriptedLLM, tool_call
from fiscal_harness.tools import Scope, ToolError, Toolbox, stage_uploads, tool_specs

from .conftest import TODAY, say
from .test_invoice_files import KV
from .test_ocr import OCR_DIGITAL, fake, png

DIGITAL_ID = "26312000000000000001"
KV_ID = "012345678901-12345678"


def text_invoice():
    return inf.parse_text(KV, "kv.pdf")


def scan_invoice():
    return inf.parse_bytes("scan.png", png(), ocr=fake(OCR_DIGITAL))


def upload(conn, text, parsed, user="U001", company="C001"):
    """一条带附件的用户消息；返回工具箱和暂存编号。"""
    sid = harness.start_session(conn, user, company)
    say(conn, sid, company, text + "\n[附件] " + "、".join(p.file_name for p in parsed))
    mid = conn.execute("SELECT MAX(id) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
    scope = Scope(sid, user, company, TODAY)
    return Toolbox(conn, scope), stage_uploads(conn, scope, mid, parsed)


def stored(conn, invoice_id, company="C001"):
    return conn.execute("SELECT COUNT(*) FROM invoices WHERE company_id=? AND invoice_id=?",
                        (company, invoice_id)).fetchone()[0]


def refused(box, ids, quote, code="NOT_AUTHORIZED"):
    with pytest.raises(ToolError) as e:
        box.import_uploaded_invoices(ids, user_quote=quote)
    assert e.value.code == code, e.value
    return e.value


# ---------- 上传只暂存，模型看到程序预检 ----------

def test_upload_is_staged_and_shown_with_precheck(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([{"content": "识别到 1 张发票，可以导入。需要导入吗？"}])
    harness.run_turn(conn, llm, settings, sid, "看看这张发票", attachments=[text_invoice()])
    up = conn.execute("SELECT * FROM uploads WHERE session_id=?", (sid,)).fetchone()
    assert up["status"] == "staged" and up["file_name"] == "kv.pdf" and stored(conn, KV_ID) == 0
    msg = conn.execute("SELECT content FROM messages WHERE session_id=? AND role='user'", (sid,)).fetchone()[0]
    assert msg == "看看这张发票\n[附件] kv.pdf"
    system = llm.calls[0][0]["content"]
    assert f"[{up['upload_id']}] kv.pdf：发票 {KV_ID}" in system and "预检：可以导入" in system


def test_rejected_upload_shows_reason_and_is_not_imported(conn):
    foreign = inf.parse_text(KV.replace("购买方名称: 甲企业", "购买方名称: 某某公司"), "other.pdf")
    box, ids = upload(conn, "导入这张发票", [foreign])
    out = box.import_uploaded_invoices(ids, user_quote="导入这张发票")
    assert out["imported"] == [] and out["not_imported"][0]["status"] == "不能导入"
    assert any("购买方" in x for x in out["not_imported"][0]["issues"]) and stored(conn, KV_ID) == 0
    assert conn.execute("SELECT status FROM uploads WHERE upload_id=?", (ids[0],)).fetchone()[0] == "rejected"


def test_image_without_ocr_explains_why(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    llm = ScriptedLLM([{"content": "这张图片需要开启本机识别才能导入。"}])
    harness.run_turn(conn, llm, settings, sid, "导入", attachments=[inf.parse_bytes("photo.jpg", png())])
    system = llm.calls[0][0]["content"]
    assert "photo.jpg：未识别出发票号码；预检：不能导入" in system and inf.NO_TEXT in system


# ---------- 导入要用户本轮明确要求 ----------

def test_explicit_request_imports_text_invoice_once(conn):
    box, ids = upload(conn, "帮我把这张发票导入", [text_invoice()])
    out = box.import_uploaded_invoices(ids, user_quote="帮我把这张发票导入")
    assert [x["invoice_id"] for x in out["imported"]] == [KV_ID] and stored(conn, KV_ID) == 1
    refused(box, ids, "帮我把这张发票导入", code="ALREADY_HANDLED")


@pytest.mark.parametrize("text", ["看看这张发票", "先别导入，我再核对一下", "如果经理同意再导入",
                                  "这张可以导入吗？", "我只是举个例子：导入这张发票", "这张发票是哪家开的"])
def test_unclear_requests_do_not_import(conn, text):
    box, ids = upload(conn, text, [text_invoice()])
    refused(box, ids, text)
    assert stored(conn, KV_ID) == 0


def test_quote_must_come_from_this_turn(conn):
    box, ids = upload(conn, "导入这张发票", [text_invoice()])
    say(conn, box.scope.session_id, "C001", "查一下9月发票总额")
    refused(box, ids, "导入这张发票")
    assert stored(conn, KV_ID) == 0


def test_file_names_are_not_the_users_words(conn):
    box, ids = upload(conn, "这是什么票", [inf.parse_text(KV, "请导入.pdf")])
    refused(box, ids, "请导入")


def test_read_only_user_and_other_sessions_cannot_import(conn):
    box, ids = upload(conn, "导入这张发票", [text_invoice()], user="U002", company="C001")  # U002 对甲企业仅查看
    refused(box, ids, "导入这张发票", code="FORBIDDEN")
    other, _ = upload(conn, "导入这张发票", [])
    refused(other, ids, "导入这张发票", code="NOT_FOUND")  # 别的会话上传的文件
    assert stored(conn, KV_ID) == 0


# ---------- 识别件：列出字段请用户核对，下一句确认才入库 ----------

def test_recognized_invoice_imports_after_next_message_confirms(conn):
    box, ids = upload(conn, "导入这张发票", [scan_invoice()])
    out = box.import_uploaded_invoices(ids, user_quote="导入这张发票")
    check = out["awaiting_check"][0]
    assert out["imported"] == [] and "check_instruction" in out and stored(conn, DIGITAL_ID) == 0
    assert (check["recognized"], check["invoice_no"], check["issue_date"], check["total"]) == \
        (True, DIGITAL_ID, "2026-09-15", "1691.23")
    say(conn, box.scope.session_id, "C001", "核对过了，没问题")
    out = box.import_uploaded_invoices(ids, user_quote="核对过了，没问题")
    assert [x["invoice_id"] for x in out["imported"]] == [DIGITAL_ID] and stored(conn, DIGITAL_ID) == 1


@pytest.mark.parametrize("reply", ["日期不对，应该是9月16日", "不对", "先别导入"])
def test_dissent_does_not_confirm(conn, reply):
    box, ids = upload(conn, "导入这张发票", [scan_invoice()])
    box.import_uploaded_invoices(ids, user_quote="导入这张发票")
    say(conn, box.scope.session_id, "C001", reply)
    refused(box, ids, reply)
    assert stored(conn, DIGITAL_ID) == 0


def test_confirmation_must_be_the_very_next_message(conn):
    box, ids = upload(conn, "导入这张发票", [scan_invoice()])
    box.import_uploaded_invoices(ids, user_quote="导入这张发票")
    say(conn, box.scope.session_id, "C001", "9月发票总额多少")
    say(conn, box.scope.session_id, "C001", "确认")
    refused(box, ids, "确认")
    assert stored(conn, DIGITAL_ID) == 0


def test_repeating_the_request_asks_again_instead_of_importing(conn):
    box, ids = upload(conn, "导入这张发票", [scan_invoice()])
    box.import_uploaded_invoices(ids, user_quote="导入这张发票")
    say(conn, box.scope.session_id, "C001", "导入")  # 隔了一轮又说导入：仍须重新核对
    say(conn, box.scope.session_id, "C001", "导入这张发票")
    out = box.import_uploaded_invoices(ids, user_quote="导入这张发票")
    assert out["awaiting_check"] and stored(conn, DIGITAL_ID) == 0


def test_mixed_batch_imports_text_invoice_and_holds_scan(conn):
    box, ids = upload(conn, "这两张都导入", [text_invoice(), scan_invoice()])
    out = box.import_uploaded_invoices(ids, user_quote="这两张都导入")
    assert [x["upload_id"] for x in out["imported"]] == [ids[0]]
    assert [x["upload_id"] for x in out["awaiting_check"]] == [ids[1]]
    assert stored(conn, KV_ID) == 1 and stored(conn, DIGITAL_ID) == 0


def test_end_to_end_turns_with_scripted_model(conn, settings):
    sid = harness.start_session(conn, "U001", "C001")
    harness.run_turn(conn, ScriptedLLM([{"content": "识别出 1 张。"}]), settings, sid, "看看", attachments=[scan_invoice()])
    uid = conn.execute("SELECT upload_id FROM uploads WHERE session_id=?", (sid,)).fetchone()[0]
    ask = harness.run_turn(conn, ScriptedLLM([
        {"tool_calls": [tool_call("import_uploaded_invoices", {"upload_ids": [uid], "user_quote": "导入吧"})]},
        {"content": "请对照票面核对号码和日期。"}]), settings, sid, "导入吧")
    assert ask.tool_calls[0]["ok"] and stored(conn, DIGITAL_ID) == 0
    llm = ScriptedLLM([
        {"tool_calls": [tool_call("import_uploaded_invoices", {"upload_ids": [uid], "user_quote": "号码日期都对"})]},
        {"content": "已导入。"}])
    harness.run_turn(conn, llm, settings, sid, "号码日期都对")
    assert "已请用户核对票面，等用户确认" in llm.calls[0][0]["content"] and stored(conn, DIGITAL_ID) == 1
    assert "import_uploaded_invoices" in {t["function"]["name"] for t in tool_specs("model_rules")}


# ---------- 网页：消息可附带文件 ----------

@pytest.fixture
def chat_api(settings, tmp_path, monkeypatch):
    s = dataclasses.replace(settings, db_path=tmp_path / "web.db", ocr="")
    c = db.connect(s.db_path)
    importer.reset_and_import(c, s.sample_xlsx, s.rules_docx)
    monkeypatch.setattr(web, "from_settings", lambda _: ScriptedLLM([{"content": "收到。"}] * 3))
    app = web.create_app(s)
    routes = {(r.path, next(iter(r.methods))): r.endpoint for r in app.routes if hasattr(r, "methods")}
    return lambda path, method="GET": routes[(path, method)], c


def _file(name, data):
    return web.UploadedFile(name=name, data=base64.b64encode(data).decode())


def test_web_message_with_files(chat_api):
    api, c = chat_api
    sid = api("/api/sessions", "POST")(web.NewSession(user_id="U001", company_id="C001"))["session_id"]
    send = api("/api/sessions/{sid}/messages", "POST")
    res = send(sid, web.Message(files=[_file("kv.pdf", KV.encode()), _file("photo.png", png())]))
    assert res["status"] == "completed"
    assert [r["file_name"] for r in c.execute("SELECT file_name FROM uploads WHERE session_id=? ORDER BY rowid", (sid,))] \
        == ["kv.pdf", "photo.png"]
    assert c.execute("SELECT content FROM messages WHERE session_id=? AND role='user'", (sid,)).fetchone()[0] \
        == "上传了 2 个发票文件\n[附件] kv.pdf、photo.png"
    with pytest.raises(HTTPException) as e:
        send(sid, web.Message(files=[_file(f"{i}.png", png()) for i in range(11)]))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        send(sid, web.Message(text=""))
    assert e.value.status_code == 400


def test_web_upload_needs_access_to_company(chat_api):
    api, c = chat_api
    sid = api("/api/sessions", "POST")(web.NewSession(user_id="U002", company_id="C002"))["session_id"]  # 无权访问
    with pytest.raises(HTTPException) as e:
        api("/api/sessions/{sid}/messages", "POST")(sid, web.Message(text="导入", files=[_file("kv.pdf", KV.encode())]))
    assert e.value.status_code == 403
    assert c.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0
