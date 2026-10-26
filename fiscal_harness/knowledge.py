"""规则知识库：导入、查找、更新（新增版本）、停用，以及按企业、业务日期、状态解析适用版本。

模型只能引用这里解析出来的规则版本；用途检查的金额门槛作为程序参数执行，不交给模型判断。
"""
from __future__ import annotations

import json
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .money import to_cents

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
HEADING = re.compile(r"^(R\d{3})\s+(.+?)\s+([\d.]+)\s*版")
THRESHOLD = re.compile(r"大于或等于\s*([\d,]+(?:\.\d+)?)\s*元时必须填写用途")
ALL_REQUIRED = re.compile(r"每张发票都必须填写用途|不设置金额门槛")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def infer_topic(name: str) -> str:
    if "用途" in name:
        return "purpose"
    if "金额" in name:
        return "amount"
    if "银行" in name:
        return "bank"
    return "general"


def infer_params(topic: str, body: str) -> dict:
    """从规则正文提取程序可执行的参数。提取不到时留空，由 CLI --param 显式补充。"""
    if topic != "purpose":
        return {}
    if ALL_REQUIRED.search(body):
        return {"purpose_required_min_cents": 0}
    m = THRESHOLD.search(body)
    if m:
        return {"purpose_required_min_cents": to_cents(m.group(1).replace(",", ""))}
    return {}


def _companies(text: str, name_map: dict[str, str]) -> list[str]:
    ids = re.findall(r"C\d{3}", text)
    if not ids:
        ids = [cid for name, cid in name_map.items() if name in text]
    return sorted(set(ids))


def parse_rules_docx(path: Path, name_map: dict[str, str]) -> list[dict]:
    """解析《业务规则》docx：清单表给元数据，“R00x 名称 x.x版”标题下的段落为正文。"""
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    meta: dict[tuple[str, str], dict] = {}
    sections: dict[tuple[str, str], list[str]] = {}
    current = None
    for el in root.find(W + "body"):
        if el.tag == W + "tbl":
            rows = [[("".join(t.text or "" for t in tc.iter(W + "t"))).strip() for tc in tr.iter(W + "tc")]
                    for tr in el.iter(W + "tr")]
            if rows and rows[0][:1] == ["规则编号"]:
                for rid, name, ver, scope, eff, status in (r[:6] for r in rows[1:] if len(r) >= 6):
                    meta[(rid, ver)] = {"rule_id": rid, "name": name, "version": ver,
                                        "companies": _companies(scope, name_map), "effective_from": eff,
                                        "status": "disabled" if "停用" in status else "active"}
            continue
        if el.tag != W + "p":
            continue
        text = "".join(t.text or "" for t in el.iter(W + "t")).strip()
        style = el.find(f"{W}pPr/{W}pStyle")
        if style is not None and style.get(W + "val", "").startswith("Heading"):
            m = HEADING.match(text)
            current = (m.group(1), m.group(3)) if m else None
            if current:
                sections[current] = []
            continue
        if current and text:
            sections[current].append(text.lstrip("•").strip())
    rules = []
    for key, info in meta.items():
        body = "\n".join(sections.get(key, []))
        if not body:
            raise ValueError(f"规则 {key[0]} {key[1]} 版在文档中没有正文")
        topic = infer_topic(info["name"])
        rules.append({**info, "topic": topic, "body": body, "params": infer_params(topic, body)})
    return rules


def upsert(conn: sqlite3.Connection, rule: dict, source: str) -> str:
    """导入一个规则版本。已存在且内容相同则跳过；内容不同则覆盖并保留状态（导入即“以文件为准”）。"""
    row = conn.execute("SELECT body, status, params FROM rule_versions WHERE rule_id=? AND version=?",
                       (rule["rule_id"], rule["version"])).fetchone()
    params = json.dumps(rule.get("params") or {}, ensure_ascii=False)
    if row and row["body"] == rule["body"] and row["status"] == rule["status"] and row["params"] == params:
        return "unchanged"
    now = _now()
    conn.execute(
        """INSERT INTO rule_versions (rule_id, version, name, topic, companies, effective_from, status, body,
                                      params, source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(rule_id, version) DO UPDATE SET name=excluded.name, topic=excluded.topic,
               companies=excluded.companies, effective_from=excluded.effective_from, status=excluded.status,
               body=excluded.body, params=excluded.params, source=excluded.source, updated_at=excluded.updated_at""",
        (rule["rule_id"], rule["version"], rule["name"], rule.get("topic") or infer_topic(rule["name"]),
         json.dumps(rule["companies"]), rule["effective_from"], rule.get("status", "active"), rule["body"],
         params, source, now, now))
    return "updated" if row else "added"


def import_file(conn: sqlite3.Connection, path: Path) -> list[tuple[str, str, str]]:
    name_map = {r["short_name"]: r["company_id"] for r in conn.execute("SELECT * FROM companies")}
    if path.suffix.lower() == ".docx":
        rules = parse_rules_docx(path, name_map)
    elif path.suffix.lower() == ".json":
        rules = json.loads(path.read_text(encoding="utf-8"))
        for r in rules:
            r.setdefault("topic", infer_topic(r["name"]))
            r.setdefault("params", infer_params(r["topic"], r["body"]))
    else:
        raise ValueError("只支持 .docx（《业务规则》格式）或 .json 规则文件")
    conn.execute("BEGIN")
    try:
        out = [(r["rule_id"], r["version"], upsert(conn, r, path.name)) for r in rules]
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def add_version(conn: sqlite3.Connection, rule_id: str, version: str, body: str, *, name: str | None = None,
                companies: list[str] | None = None, effective_from: str | None = None,
                params: dict | None = None) -> dict:
    """更新规则 = 新增版本；未给出的元数据沿用该规则最近一个版本。"""
    prev = conn.execute("SELECT * FROM rule_versions WHERE rule_id=? ORDER BY effective_from DESC, created_at DESC",
                        (rule_id,)).fetchone()
    if conn.execute("SELECT 1 FROM rule_versions WHERE rule_id=? AND version=?", (rule_id, version)).fetchone():
        raise ValueError(f"{rule_id} {version} 版已存在，请使用新的版本号")
    if prev is None and not (name and companies and effective_from):
        raise ValueError("新规则必须提供名称、适用企业和生效日期")
    name = name or prev["name"]
    topic = infer_topic(name)
    rule = {"rule_id": rule_id, "version": version, "name": name, "topic": topic,
            "companies": companies or json.loads(prev["companies"]),
            "effective_from": effective_from or date.today().isoformat(), "status": "active", "body": body,
            "params": params if params is not None else infer_params(topic, body)}
    date.fromisoformat(rule["effective_from"])
    upsert(conn, rule, "cli")
    return rule


def set_status(conn: sqlite3.Connection, rule_id: str, version: str, status: str) -> None:
    cur = conn.execute("UPDATE rule_versions SET status=?, updated_at=? WHERE rule_id=? AND version=?",
                       (status, _now(), rule_id, version))
    if cur.rowcount == 0:
        raise ValueError(f"找不到规则 {rule_id} {version} 版")


@dataclass
class Resolved:
    chosen: sqlite3.Row | None
    excluded: list[dict]

    def ref(self) -> dict | None:
        return ref_of(self.chosen) if self.chosen else None


def ref_of(row: sqlite3.Row) -> dict:
    return {"rule_id": row["rule_id"], "name": row["name"], "version": row["version"],
            "effective_from": row["effective_from"]}


def resolve(conn: sqlite3.Connection, company_id: str, *, as_of: date, topic: str | None = None,
            rule_id: str | None = None) -> list[Resolved]:
    """每个规则编号选出一个适用版本：适用企业包含当前企业、生效日期不晚于业务日期、状态为启用，取生效最晚的。"""
    sql, args = "SELECT * FROM rule_versions WHERE 1=1", []
    if topic:
        sql, args = sql + " AND topic=?", args + [topic]
    if rule_id:
        sql, args = sql + " AND rule_id=?", args + [rule_id]
    by_rule: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(sql + " ORDER BY rule_id, effective_from DESC, version DESC", args):
        by_rule.setdefault(row["rule_id"], []).append(row)
    out = []
    for rows in by_rule.values():
        chosen, excluded = None, []
        for row in rows:
            ver = f'{row["rule_id"]} {row["name"]} {row["version"]} 版'
            if company_id not in json.loads(row["companies"]):
                continue  # 其他企业的规则不列入排除说明，避免把无关规则带进上下文
            if row["status"] != "active":
                excluded.append({"rule": ver, "reason": "已停用"})
            elif date.fromisoformat(row["effective_from"]) > as_of:
                excluded.append({"rule": ver, "reason": f'{row["effective_from"]} 才生效'})
            elif chosen is None:
                chosen = row
            else:
                excluded.append({"rule": ver, "reason": f'已被 {chosen["version"]} 版取代'})
        if chosen or excluded:
            out.append(Resolved(chosen, excluded))
    return out


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text)
    return {text[i:i + 2] for i in range(len(text) - 1)} | set(re.findall(r"[A-Za-z0-9.]+", text))


def search(conn: sqlite3.Connection, company_id: str, query: str, *, as_of: date, limit: int = 4) -> dict:
    """只在适用版本中按中文二元组重合度排序，返回条款原文片段。"""
    resolved = resolve(conn, company_id, as_of=as_of)
    q = _bigrams(query)
    hits = []
    for r in resolved:
        if not r.chosen:
            continue
        for i, para in enumerate(r.chosen["body"].split("\n"), start=1):
            score = len(q & _bigrams(r.chosen["name"] + para))
            if score:
                hits.append((score, {**ref_of(r.chosen), "clause": i, "text": para}))
    hits.sort(key=lambda h: -h[0])
    return {"as_of": as_of.isoformat(),
            "snippets": [h[1] for h in hits[:limit]],
            "excluded_versions": [e for r in resolved for e in r.excluded]}


def full_text(conn: sqlite3.Connection, company_id: str, rule_id: str, *, as_of: date) -> dict | None:
    for r in resolve(conn, company_id, as_of=as_of, rule_id=rule_id):
        if r.chosen:
            return {**ref_of(r.chosen), "clauses": r.chosen["body"].split("\n"),
                    "excluded_versions": r.excluded}
    return None
