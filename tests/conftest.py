from datetime import date

import pytest

from fiscal_harness import db, harness, importer
from fiscal_harness.config import load_settings
from fiscal_harness.tools import Scope, Toolbox

TODAY = date(2026, 10, 26)


@pytest.fixture
def settings():
    s = load_settings()
    return s.__class__(**{**s.__dict__, "today": TODAY})


@pytest.fixture
def conn(settings):
    c = db.connect(":memory:")
    importer.reset_and_import(c, settings.sample_xlsx, settings.rules_docx)
    return c


def say(conn, session_id, company_id, text):
    """模拟用户在会话中说过的话（守卫依据会话原话判断月份与修改授权）。"""
    conn.execute("INSERT INTO messages (session_id, company_id, role, content, created_at) VALUES (?,?,?,?,?)",
                 (session_id, company_id, "user", text, "2026-10-26T10:00:00"))


@pytest.fixture
def box_for(conn):
    def make(user="U001", company="C001", said=("查一下2026年9月和8月、7月的资料",)):
        sid = harness.start_session(conn, user, company)
        for t in said:
            say(conn, sid, company, t)
        return Toolbox(conn, Scope(sid, user, company, TODAY))
    return make
