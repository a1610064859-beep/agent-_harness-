"""写入前对用户原话做的两类检查。是否要修改、保存、记住由模型理解；这里不判断用户“想做什么”。

- 风险表述：否定、条件、不确定、举例、句末问句。出现时不直接写入，转为请用户确认（只拦，不放行）。
- 值的对应：“F002和F003分别填A和B”按顺序拆出每张发票的值，核对模型给的值确实是用户给这张发票的
  （逐张对应由 tools.Toolbox._value_bound 完成）；记忆内容按字面重合度核对出处。
"""
from __future__ import annotations

import re

# “分别保存”里的“别”不是否定，用后顾排除常见的“X别”词
NEGATION = re.compile(r"(?<![分区特级识类告派性])(别|不要|不用|不必|勿|先不|暂不|暂时不|还不|无需|没让你)"
                      r"(急着?)?(实际)?(去)?(保存|写入|提交|修改|改|操作|执行|动|填|写|导入|入库|录入|记|删)")
CONDITION = re.compile(r"如果|假如|要是|倘若|万一|若是|的话|等[^，。；,;]{0,12}(再|以后|之后|后)"
                       r"|(同意|批准|核实|审批|签字|确认)了?(以|之)?后")
UNCERTAIN = re.compile(r"可能|也许|大概|好像|似乎|不确定|不太确定|不清楚|没核实|未核实|待核实|待确认|没确认|还没定|看看|随便")
EXAMPLE = re.compile(r"举例|举个例子|例如|比如|打个比方|示例|假设|模拟一下|演示一下")
# 之后的话撤回或推翻之前的要求：之前那句原话不再作为授权
REVOKE = re.compile(r"算了|撤销|撤回|取消|作废|收回|说错了|搞错了|弄错了|改主意|不改了|不用改了|不保存了|别动")
QUESTION = re.compile(r"[吗么][？?。]?$|[？?]$")
VALUE_NEGATED = re.compile(r"(不是|不要|不用|不能|别|不填|不写|而非|而不是|不再是)[填写为是成]?$")

SPLIT_EACH = re.compile(r"分别")
SAME_FOR_ALL = re.compile(r"都|均|全部|一样|同样|统一")
PARTS = re.compile(r"和|与|以及|及|跟|、|/")
LEAD_VERB = re.compile(r"^(的?(用途|备注))?(都|均)?(填写|填成|填为|填|写成|写为|写|改成|改为|改|为|是|记为|记)[:：]?")

# 识别件的核对：用户对列出的字段提出异议时不导入；“没有错误”“没错了”这类否定加否定不算异议
DISSENT = re.compile(r"不(可以|行|对|是|同意|确认|一致|符)|有误|有错|错了|不符|对不上|不一样")
NO_PROBLEM = re.compile(r"(没有|没|无|并无|不存在|未发现|没发现)(什么|任何)?(错误?了?|有误|不一致|不符|不对|对不上|不一样|问题|出入)")


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
    if QUESTION.search(t):
        out.append("question")
    return out


def revokes(text: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    return bool(REVOKE.search(t) or NEGATION.search(t))


def dissents(text: str) -> bool:
    return bool(DISSENT.search(NO_PROBLEM.sub("", re.sub(r"\s+", "", text or ""))))


RISK_TEXT = {"negation": "表示暂不保存或不要修改", "condition": "带有条件（如“如果…再…”）",
             "uncertain": "表示不确定或只是让你看看", "example": "只是举例或假设", "question": "是在询问",
             "ambiguous": "多张发票的内容不能逐一对应"}


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
