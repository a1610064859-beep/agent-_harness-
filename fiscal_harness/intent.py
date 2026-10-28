"""写入授权用到的原话分析：只用来拒绝或要求确认，从不用来放宽授权。

- 风险表述：否定、条件、不确定、举例。出现时不直接写入。
- “分别”句式：按顺序拆出每张发票的值（逐张对应由 tools.Toolbox._value_bound 完成）。
- 记忆请求：保存、更正、删除偏好都要有本轮用户明确的话，内容要能在原话里找到出处。
- 导入请求：对话中上传的发票要用户本轮明确要求导入；识别件的核对确认里不能有异议。
"""
from __future__ import annotations

import re

# “分别保存”里的“别”不是否定，用后顾排除常见的“X别”词
NEGATION = re.compile(r"(?<![分区特级识类告派性])(别|不要|不用|不必|勿|先不|暂不|暂时不|还不|无需|没让你)"
                      r"(急着?)?(实际)?(去)?(保存|写入|提交|修改|改|操作|执行|动|填|写|导入|入库|录入)")
CONDITION = re.compile(r"如果|假如|要是|倘若|万一|若是|的话|等[^，。；,;]{0,12}(再|以后|之后|后)"
                       r"|(同意|批准|核实|审批|签字|确认)了?(以|之)?后")
UNCERTAIN = re.compile(r"可能|也许|大概|好像|似乎|不确定|不太确定|不清楚|没核实|未核实|待核实|待确认|没确认|还没定|看看|随便")
EXAMPLE = re.compile(r"举例|举个例子|例如|比如|打个比方|示例|假设|模拟一下|演示一下")
ACTION = re.compile(r"保存|存一下|存上|写入|提交|改成|改为|改一下|更正|修改|填|写|记上|补上|补充|录入|更新|设为|设置为|换成")
SAVE = re.compile(r"保存|存一下|存上|写入|提交|确认修改|确认保存|改吧|改上|填上|就这么改|按这个改")
AFFIRM = re.compile(r"确认|可以|好的|没问题|同意|就这样|就按|^行|行吧|对的|是的|没错")
VALUE_NEGATED = re.compile(r"(不是|不要|不用|不能|别|不填|不写|而非|而不是|不再是)[填写为是成]?$")

SPLIT_EACH = re.compile(r"分别")
SAME_FOR_ALL = re.compile(r"都|均|全部|一样|同样|统一")
PARTS = re.compile(r"和|与|以及|及|跟|、|/")
LEAD_VERB = re.compile(r"^(的?(用途|备注))?(都|均)?(填写|填成|填为|填|写成|写为|写|改成|改为|改|为|是|记为|记)[:：]?")

IMPORT = re.compile(r"导入|入库|录入|登记|入账|保存|存进|加进|加到|添加")
# 识别件的核对确认：要说到核对或无误，单说“导入”不算核对过
CHECKED = re.compile(r"核对(过|了|好|完|无误)|对过了|看过了|一致|无误|都对|没错|没问题|确认|正确|准确")
QUESTION = re.compile(r"吗[？?。]?$|[？?]$|能不能|可不可以|是不是|是否|能否|行不行|要不要")
# 客气的请求（“能不能帮我把这几张导入”）不是询问；“帮我看看能不能导入”仍是询问
POLITE_IMPORT = re.compile(r"(?:^|[，。,;；！!])(能不能|可不可以|能否|可否|可以|麻烦你?|请你?)(帮我|帮忙|替我|给我)"
                           r"(把[^，。,;；？?吗]{0,20})?(导入|入库|录入|登记)(一下)?(吧|呢)?吗?[？?。]?$")
DISSENT = re.compile(r"不(可以|行|对|是|同意|确认|一致|符)|有误|有错|错了|不符|对不上|不一样")
# “没有错误”“没错了”“没有不一致的地方”是确认，判异议前先去掉
NO_PROBLEM = re.compile(r"(没有|没|无|并无|不存在|未发现|没发现)(什么|任何)?(错误?了?|有误|不一致|不符|不对|对不上|不一样|问题|出入)")
DISCARD = re.compile(r"不导入|不要导入|别导入|不用导入|先不导入|暂不导入|不入库|取消导入|放弃|不要了|算了|作废|删掉|删除|去掉|撤回")

REMEMBER = re.compile(r"记住|记下|记一下|记着|以后|今后|往后|之后都|每次|一直|默认|始终|长期")
ALL_COMPANIES = re.compile(r"所有企业|全部企业|每家企业|各家?企业|所有公司|每家公司|跨企业|两家|不管哪家|不论哪家|都适用|通用")
EDIT = re.compile(r"改|更正|换成|调整|更新")
RECENT = re.compile(r"刚才|刚刚|上一条|最近那条|最新那条")
DELETE = re.compile(r"删|忘|去掉|取消|撤销|清除|不用记|别记|不要记|不再需要")


def risks(text: str) -> list[str]:
    """原话中让“直接写入”不可靠的表述。"""
    t = re.sub(r"\s+", "", text or "")
    out = []
    if NEGATION.search(t):
        out.append("negation")
    if CONDITION.search(t):
        out.append("condition")
    if UNCERTAIN.search(t):
        out.append("uncertain")
    if EXAMPLE.search(t):
        out.append("example")
    return out


def dissents(text: str) -> bool:
    """原话对识别结果提出了异议；“没有错误”“没错了”这类否定加否定不算。"""
    return bool(DISSENT.search(NO_PROBLEM.sub("", re.sub(r"\s+", "", text or ""))))


def asks(text: str) -> bool:
    """原话是在询问（“可以导入吗？”），不是请求；“能不能帮我把这几张导入”算请求。"""
    t = re.sub(r"\s+", "", text or "")
    return bool(QUESTION.search(t)) and not POLITE_IMPORT.search(t)


RISK_TEXT = {"negation": "表示暂不保存或不要修改", "condition": "带有条件（如“如果…再…”）",
             "uncertain": "表示不确定或只是让你看看", "example": "只是举例或假设",
             "no_action": "没有要求修改或保存", "ambiguous": "多张发票的内容不能逐一对应"}


def split_each(seg: str) -> list[str]:
    """“用途分别填打印纸、硒鼓和合同邮寄，保存” → ["打印纸", "硒鼓", "合同邮寄"]；seg 保留原标点。
    值列表到第一个句读为止；用逗号隔开的值分不清是不是同一句，拆出的个数对不上时由调用方按含糊处理。"""
    after = SPLIT_EACH.split(seg, maxsplit=1)[1].strip()
    after = LEAD_VERB.sub("", after)
    clause = re.split(r"[，。；,;！!？?]", after, maxsplit=1)[0]
    parts = [x.strip() for x in PARTS.split(clause)]
    if parts:  # 没有句读时，最后一项后面常跟着“保存”“请保存”等要求，去掉
        parts[-1] = re.split(r"请|帮我|并|然后|再|保存|提交", parts[-1])[0]
    return [x for x in parts if x]


def coverage(content: str, source: str, norm) -> float:
    """content 的中文二元组有多少出现在 source 中，用来判断记忆内容是否出自用户原话。"""
    c, s = norm(content), norm(source)
    if not c:
        return 0.0
    if len(c) == 1:
        return 1.0 if c in s else 0.0
    grams = {c[i:i + 2] for i in range(len(c) - 1)}
    return sum(g in s for g in grams) / len(grams)
