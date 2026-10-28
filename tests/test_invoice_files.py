"""发票文件导入：解析、校验与入库。票面文字按真实版式构造，企业与税号均为虚构，不依赖外部文件和模型。"""
import io
import zipfile

import pytest

from fiscal_harness import invoice_files as inf
from fiscal_harness.tools import ToolError

DIGITAL = """电子发票（增值税专用发票） 发票号码： 2 6 3 1 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1
开票日期： 2026年 09月 15日
购
买
方
信
息
销
售
方
信
息
名称：甲企业 名称：测试广告有限公司
统一社会信用代码 /纳税人识别号： 91310101MA1CTEN01X 统一社会信用代码 /纳税人识别号： 91310101MA1CTEN02Y
项目名称 规格型号 单  位 数  量 单  价 金  额 税率 /征收率 税  额
*文化服务 *广告发布费 项 1 1595.50 1595.50 6% 95.73
合  计 ¥1595.50 ¥95.73
价税合计（大写） 壹仟陆佰玖拾壹圆贰角叁分 （小写） ¥1691.23
备
注
合同号  HT001
开票人：测试员
"""

LEGACY = """合 计
天津增值税电子普通发票
价税合计（大写） （小写）
发票代码:
发票号码:
￥734.59 ￥22.04
*运输服务*客运服务费 无 次 1 734.59 734.59 3% 22.04
499099801283
柒佰伍拾陆元陆角叁分
03392/<9405<-48<-9/7-376018>
*+<*85>6/901++2319681*-//323
甲企业
91330100MA2CTEN01X
测试地址 0571-00000000
测试银行 000000
测试出行有限公司
91120116MA2CTEN02Y
测试地址二 022-00000000
测试银行二 111111
012001800211
23326471
2018年09月21日
16707 18454 90097 07698
￥756.63
"""

KV = """增值税电子普通发票
发票代码: 012345678901
发票号码: 12345678
开票日期: 2026年09月20日
购买方名称: 甲企业
购买方纳税人识别号: 91330100MA2CTEN01X
销售方名称: 测试服务有限公司
销售方纳税人识别号: 91440300MA2CTEN03W
项目名称|规格型号|单位|数量|单价|金额|税率|税额
*信息技术服务*软件服务|标准版|次|1|100.00|100.00|13%|13.00
税额: 13.00
价税合计（大写）: 壹佰壹拾叁圆整
价税合计: 113.00
"""


@pytest.mark.parametrize("text,cents", [
    ("壹仟陆佰玖拾壹圆贰角叁分", 169123), ("负贰仟壹佰捌拾圆整", -218000), ("玖万肆仟贰佰贰拾贰圆贰角壹分", 9422221),
    ("柒佰伍拾陆元陆角叁分", 75663), ("叁佰零肆圆伍角整", 30450), ("拾伍元整", 1500), ("壹亿零贰万圆整", 10002000000),
    ("伍角", 50), ("壹佰元零伍分", 10005),
])
def test_upper_to_cents(text, cents):
    assert inf.upper_to_cents(text) == cents


def test_upper_rejects_unknown_chars():
    with pytest.raises(ValueError):
        inf.upper_to_cents("壹佰元abc")


def test_digital_invoice():
    i = inf.parse_text(DIGITAL)
    assert (i.layout, i.issues) == ("pdf_digital", [])
    assert i.invoice_id == "26312000000000000001"  # 号码中间的空格被去掉
    assert (i.issue_date, i.buyer_name, i.seller_name) == ("2026-09-15", "甲企业", "测试广告有限公司")
    assert (i.buyer_tax_id, i.seller_tax_id) == ("91310101MA1CTEN01X", "91310101MA1CTEN02Y")
    assert (i.amount_cents, i.tax_cents, i.total_cents) == (159550, 9573, 169123)
    assert i.items == ["*文化服务*广告发布费"]
    assert i.face_remark == "合同号  HT001"


def test_digital_personal_buyer_and_no_remark():
    text = DIGITAL.replace("名称：甲企业", "名称：个人").replace("纳税人识别号： 91310101MA1CTEN01X ", "纳税人识别号： ")
    text = text.replace("合同号  HT001\n", "")
    i = inf.parse_text(text)
    assert (i.buyer_name, i.buyer_tax_id, i.face_remark, i.issues) == ("个人", None, None, [])


def test_red_invoice_is_noted_not_rejected():
    text = (DIGITAL.replace("1595.50", "-1595.50").replace("95.73", "-95.73").replace("¥1691.23", "¥-1691.23")
            .replace("壹仟陆佰玖拾壹圆贰角叁分", "负壹仟陆佰玖拾壹圆贰角叁分"))
    i = inf.parse_text(text)
    assert i.issues == [] and i.total_cents == -169123
    assert any("红字" in n for n in i.notes)


def test_legacy_invoice():
    i = inf.parse_text(LEGACY)
    assert (i.layout, i.issues) == ("pdf_legacy", [])
    assert i.invoice_id == "012001800211-23326471"  # 机器编号同为 12 位，不能误当发票代码
    assert (i.buyer_name, i.seller_name) == ("甲企业", "测试出行有限公司")
    assert (i.amount_cents, i.tax_cents, i.total_cents) == (73459, 2204, 75663)
    assert i.items == ["*运输服务*客运服务费"]  # 密码区以 * 开头的行不算项目


def test_kv_invoice():
    i = inf.parse_text(KV)
    assert (i.layout, i.issues) == ("text_kv", [])
    assert i.invoice_id == "012345678901-12345678"
    assert (i.amount_cents, i.tax_cents, i.total_cents) == (10000, 1300, 11300)


def test_tampered_total_is_rejected():
    i = inf.parse_text(DIGITAL.replace("（小写） ¥1691.23", "（小写） ¥1791.23"))
    assert any("≠ 价税合计" in x for x in i.issues)
    assert any("大写金额" in x for x in i.issues)


def test_missing_fields_are_rejected():
    i = inf.parse_text("随便一段文字 ¥100.00")
    assert {"未识别到发票号码", "未识别到开票日期", "未识别到价税合计"} <= set(i.issues)


def _ofd(meta_no="26312000000000000002", meta_amount="100.00"):
    texts = {"1": "26312000000000000002", "2": "2026年09月18日", "3": "甲企业", "4": "测试钢管有限公司",
             "5": "¥", "6": "100.00", "7": "¥", "8": "13.00", "9": "¥", "10": "113.00", "11": "*钢材*", "12": "焊管",
             "13": "壹佰壹拾叁圆整"}
    content = ('<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Content><ofd:Layer>' + "".join(
        f'<ofd:TextObject ID="{k}"><ofd:TextCode>{v}</ofd:TextCode></ofd:TextObject>' for k, v in texts.items())
        + "</ofd:Layer></ofd:Content></ofd:Page>")
    refs = lambda *ids: "".join(f'<ofd:ObjectRef PageRef="1">{i}</ofd:ObjectRef>' for i in ids)
    tags = ('<ofd:root xmlns:ofd="http://www.ofdspec.org/2016">'
            f"<ofd:InvoiceNo>{refs('1')}</ofd:InvoiceNo><ofd:IssueDate>{refs('2')}</ofd:IssueDate>"
            f"<ofd:Buyer><ofd:BuyerName>{refs('3')}</ofd:BuyerName></ofd:Buyer>"
            f"<ofd:Seller><ofd:SellerName>{refs('4')}</ofd:SellerName></ofd:Seller>"
            f"<ofd:TaxExclusiveTotalAmount>{refs('5', '6')}</ofd:TaxExclusiveTotalAmount>"
            f"<ofd:TaxTotalAmount>{refs('7', '8')}</ofd:TaxTotalAmount>"
            f"<ofd:TaxInclusiveTotalAmount>{refs('9', '10')}</ofd:TaxInclusiveTotalAmount>"
            f"<ofd:Item>{refs('11', '12')}</ofd:Item></ofd:root>")
    ofd = ('<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016"><ofd:DocBody><ofd:DocInfo><ofd:CustomDatas>'
           f'<ofd:CustomData Name="发票号码">{meta_no}</ofd:CustomData>'
           f'<ofd:CustomData Name="合计金额">{meta_amount}</ofd:CustomData>'
           "</ofd:CustomDatas></ofd:DocInfo></ofd:DocBody></ofd:OFD>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OFD.xml", ofd)
        z.writestr("Doc_0/Pages/Page_0/Content.xml", content)
        z.writestr("Doc_0/Tags/CustomTag.xml", tags)
    return buf.getvalue()


def test_ofd_with_semantic_tags():
    i = inf.parse_bytes("a.ofd", _ofd())
    assert (i.layout, i.issues) == ("ofd", [])
    assert (i.invoice_id, i.issue_date, i.seller_name) == ("26312000000000000002", "2026-09-18", "测试钢管有限公司")
    assert (i.amount_cents, i.tax_cents, i.total_cents, i.items) == (10000, 1300, 11300, ["*钢材*焊管"])


def test_ofd_metadata_mismatch_is_rejected():
    i = inf.parse_bytes("a.ofd", _ofd(meta_no="26312000000000000099", meta_amount="90.00"))
    assert any("元数据发票号码" in x for x in i.issues) and any("元数据合计金额" in x for x in i.issues)


def test_broken_files_become_issues():
    assert inf.parse_bytes("x.pdf", b"%PDF-1.4 broken").issues[0].startswith("文件无法解析")
    assert inf.parse_bytes("x.txt", b"hello").issues == ["不是 PDF、OFD 或图片文件"]


# ---------- 入库 ----------

def _count(conn, company="C001"):
    return conn.execute("SELECT COUNT(*) FROM invoices WHERE company_id=?", (company,)).fetchone()[0]


def test_preview_does_not_write(conn):
    before = _count(conn)
    r = inf.import_files(conn, "C001", [inf.parse_text(DIGITAL)])
    assert r[0]["status"] == "would_import" and _count(conn) == before


def test_import_duplicate_and_conflict(conn):
    before = _count(conn)
    r = inf.import_files(conn, "C001", [inf.parse_text(DIGITAL), inf.parse_text(KV)], commit=True)
    assert [x["status"] for x in r] == ["imported", "imported"] and _count(conn) == before + 2
    row = conn.execute("SELECT * FROM invoices WHERE invoice_id='26312000000000000001'").fetchone()
    assert (row["amount_cents"], row["seller"], row["purpose"], row["remark"]) == (169123, "测试广告有限公司", None, None)
    src = conn.execute("SELECT layout FROM invoice_sources WHERE invoice_id='26312000000000000001'").fetchone()
    assert src["layout"] == "pdf_digital"
    # 同一张再导入：跳过；同号不同内容：冲突，不覆盖
    assert inf.import_files(conn, "C001", [inf.parse_text(DIGITAL)], commit=True)[0]["status"] == "duplicate"
    changed = inf.parse_text(DIGITAL.replace("测试广告有限公司", "另一家公司"))
    assert inf.import_files(conn, "C001", [changed], commit=True)[0]["status"] == "conflict"
    assert _count(conn) == before + 2


def test_buyer_must_be_the_company(conn):
    other = inf.parse_text(DIGITAL.replace("名称：甲企业", "名称：别的公司"))
    assert inf.import_files(conn, "C001", [other])[0]["status"] == "rejected"
    assert inf.import_files(conn, "C001", [other], accept_buyer_mismatch=True)[0]["status"] == "would_import"
    # 甲企业的票不能导入乙企业
    assert inf.import_files(conn, "C002", [inf.parse_text(DIGITAL)])[0]["status"] == "rejected"


def test_import_requires_write_permission(conn):
    inf.require_write(conn, "U001", "C001")
    for user, company in (("U002", "C001"), ("U002", "C002"), ("U999", "C001")):
        with pytest.raises(PermissionError):
            inf.require_write(conn, user, company)


def test_red_invoice_counts_as_required_under_every_invoice_rule(conn, box_for):
    red = (DIGITAL.replace("1595.50", "-1595.50").replace("95.73", "-95.73").replace("¥1691.23", "¥-1691.23")
           .replace("壹仟陆佰玖拾壹圆贰角叁分", "负壹仟陆佰玖拾壹圆贰角叁分"))
    inf.import_files(conn, "C001", [inf.parse_text(red)], commit=True)
    r = box_for().check_purpose("2026-09")  # 甲企业 9 月 R002 2.0：每张都必须填写
    assert "26312000000000000001" in [x["invoice_id"] for x in r["missing_required"]]


def test_invoice_type():
    assert inf.parse_text(DIGITAL).invoice_type == "数电专票"
    assert inf.parse_text(DIGITAL.replace("增值税专用发票", "普通发票")).invoice_type == "数电普票"
    assert inf.parse_text(LEGACY).invoice_type == "增值税电子普通发票"  # 前面带省份名
    assert inf.parse_text(KV).invoice_type == "增值税电子普通发票"


def _red(remark=""):
    return inf.parse_text(DIGITAL.replace("0 0 0 1\n", "0 0 0 9\n")
                          .replace("1595.50", "-1595.50").replace("95.73", "-95.73").replace("¥1691.23", "¥-1691.23")
                          .replace("壹仟陆佰玖拾壹圆贰角叁分", "负壹仟陆佰玖拾壹圆贰角叁分").replace("合同号  HT001", remark))


def test_red_invoice_links_to_blue(conn):
    red = _red("被红冲蓝字数电发票号码：26312000000000000001")
    assert red.red_of == "26312000000000000001"
    # 蓝字票不在库里：提示复核
    r = inf.import_files(conn, "C001", [red])[0]
    assert r["status"] == "would_import" and any("找不到对应的蓝字发票" in n for n in r["notes"])
    # 先导入蓝字票，再导入红字票：提示冲销关系
    inf.import_files(conn, "C001", [inf.parse_text(DIGITAL)], commit=True)
    r = inf.import_files(conn, "C001", [red])[0]
    assert "冲销蓝字发票 26312000000000000001" in r["notes"]
    assert any("找不到" in n for n in inf.import_files(conn, "C001", [_red()])[0]["notes"])


def test_import_event_is_visible_to_model_but_not_user_speech(conn, box_for):
    from fiscal_harness import context
    box = box_for(said=())
    parsed = [inf.parse_text(DIGITAL)]
    results = inf.import_files(conn, "C001", parsed, commit=True)
    text = inf.record_import_event(conn, box.scope.session_id, "C001", results, parsed)
    assert "26312000000000000001" in text and "1691.23" in text
    hist = context.history(conn, box.scope, 6000)
    assert hist[-1]["content"].startswith("[系统事件]")
    # 事件里出现了 2026-09，但用户本人没说过月份：查询仍须追问，不能当作用户原话
    with pytest.raises(ToolError) as e:
        box.query_invoices(month="2026-09")
    assert e.value.code == "MONTH_UNCONFIRMED"
    # 没有导入成功的票时不记事件
    assert inf.record_import_event(conn, box.scope.session_id, "C001", [{"status": "duplicate"}], parsed) is None
